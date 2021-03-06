# -*- coding: utf-8 -*-
import csv
import datetime
import logging
import math
import os
import sys
import threading
import time
from collections import namedtuple
from contextlib import contextmanager
from queue import Queue

import requests
from awsauth import S3Auth
from flask import Flask, json, request, Response

# # # Configuration # # #

BIND_HOST = os.environ.get('BIND_HOST', '0.0.0.0')
BIND_PORT = int(os.environ.get('BIND_PORT', 5000))
ACCESS_KEY = os.environ['AWS_ACCESS_KEY_ID']
SECRET_KEY = os.environ['AWS_SECRET_KEY']
LOGGLY_TOKEN = os.environ['LOGGLY_TOKEN']
ONLY_ERRORS = os.environ.get('ONLY_ERRORS', '').lower() in ('true', '1')
MAX_TRIES = os.environ.get('MAX_TRIES', 15)  # 15 means I will give up after ~9 hours
LOGGLY_TAG = os.environ.get('LOGGLY_TAG', 'elb')
LOGGLY_MAX_SIZE = int(os.environ.get('LOGGLY_MAX_SIZE', 4*1024*1024))

loggly_url = 'https://logs-01.loggly.com/bulk/{}/{}/bulk/'.format(
    LOGGLY_TOKEN,
    LOGGLY_TAG,
)


# # # Globals # # #

app = Flask(__name__)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter('%(levelname)s: %(asctime)s: %(message)s'))
app.logger.addHandler(handler)
app.logger.setLevel(logging.DEBUG)

s3ssion = requests.Session()
s3ssion.auth = S3Auth(ACCESS_KEY, SECRET_KEY)

Task = namedtuple('Task', ['url', 'not_before', 'tries'])


class MyQueue(Queue):
    @contextmanager
    def task(self):
        try:
            yield self.get()
        finally:
            self.task_done()

q = MyQueue()

worker_thread = None

header = [
    'timestamp',
    'elb',
    'client',
    'backend',
    'request_processing_time',
    'backend_processing_time',
    'response_processing_time',
    'elb_status_code',
    'backend_status_code',
    'received_bytes',
    'sent_bytes',
    'request',
    # additional fields not available in older ELBs (pre March 2015)
    'user_agent',
    'ssl_cipher',
    'ssl_protocol'
]

processors = {
    'elb_status_code': int,
    'backend_status_code': int,
    'received_bytes': int,
    'sent_bytes': int,
}

log_format = (
    '{client_ip} {elb} {user} [{timestamp_obj:%d/%b/%Y:%H:%M:%S +0000}] '
    '"{http_method} {request_uri} {http_version}" '
    '{elb_status_code} {sent_bytes} "{referrer}" "{user_agent_or_empty}" '
)


class Dialect(csv.Dialect):
    delimiter = ' '
    quotechar = '"'
    lineterminator = '\n'
    escapechar = '\\'
    quoting = csv.QUOTE_ALL


# # # Task handling # # #

def apache_combined_log(row):
    timestamp_obj = datetime.datetime.strptime(row['timestamp'],
                                               "%Y-%m-%dT%H:%M:%S.%fZ")
    return log_format.format(
        timestamp_obj=timestamp_obj,
        user='-',
        referrer='',
        user_agent_or_empty=row['user_agent'] or '-',
        **row
    )


def upload_to_loggly(events):
    data = '\n'.join(events).encode('utf-8')
    app.logger.info(
        'Sending %d items (%s) to loggly',
        len(events), file_size(len(data))
    )
    response = requests.post(
        loggly_url,
        data='\n'.join(events),
        headers={'content-type': 'text/plain;charset=utf-8'},
        timeout=5,
    )
    if response.status_code != requests.codes.ok:
        raise Exception('Loggly responded %s' % response.text)


def download_and_process(s3_object_url):
    response = s3ssion.get(s3_object_url, timeout=5)
    reader = csv.DictReader(response.text.splitlines(), fieldnames=header,
                            restval=None, dialect=Dialect)
    output = []
    size = 0
    for row in reader:
        # split up some compound fields
        row['client_ip'], row['client_port'] = row.pop('client').split(':')
        row['http_method'], row['request_uri'], row['http_version'] = row.pop('request').split(' ')

        # convert some fields into native types
        for field, func in processors.items():
            try:
                row[field] = func(row[field])
            except Exception:
                pass
        if ONLY_ERRORS and row['elb_status_code'] < 500:
            continue
        output.append(apache_combined_log(row) + json.dumps(row))
        size += len(output[-1])
        if size > LOGGLY_MAX_SIZE:
            upload_to_loggly(output)
            output = []
            size = 0
    if output:
        upload_to_loggly(output)


def file_size(size):
    _suffixes = ['bytes', 'KiB', 'MiB', 'GiB', 'TiB', 'EiB', 'ZiB']
    order = int(math.log2(size) / 10) if size else 0
    return '{:.4g} {}'.format(size / (1 << (order * 10)), _suffixes[order])


def worker():
    while True:
        with q.task() as task:
            url, not_before, tries = task

            # check if this task should already run
            if not_before <= datetime.datetime.now():
                tries += 1
                try:
                    app.logger.info('Downloading and processing %s, tries: %d', url, tries)
                    download_and_process(url)
                    continue
                except Exception as e:
                    # back off exponentially
                    not_before = datetime.datetime.now() + datetime.timedelta(seconds=2**tries)
                    app.logger.warning(
                        'Got exception while processing %s: %s(%s). '
                        'Retrying not before %s',
                        url, type(e).__name__, str(e),
                        not_before.strftime('%H:%M:%S'),
                        exc_info=True,
                    )
            if tries <= MAX_TRIES:
                q.put(Task(url, not_before, tries))
            else:
                app.logger.error('Gave up sending %s after %d tries', url, tries)
            time.sleep(10)  # sleep a bit to not tax the CPU too much


@app.route('/sns', methods=['GET', 'POST'])
def sns():
    global worker_thread
    headers = request.headers
    if request.method == 'GET':
        return Response(
            '\n'.join('{}={}'.format(*item) for item in headers.items()),
            mimetype='text/plain',
            status=200,
        )
    msg_type = headers.get('x-amz-sns-message-type')
    app.logger.info('Got SNS notification %s; content_type=%s', msg_type, request.content_type)
    obj = json.loads(request.data)

    if msg_type == 'SubscriptionConfirmation':
        subscribe_url = obj[u'SubscribeURL']
        response = requests.get(subscribe_url)

    elif msg_type == 'UnsubscribeConfirmation':
        pass

    elif msg_type == 'Notification':
        message = json.loads(obj['Message'])
        event = message.get('Event', 'unknown')
        if 'Records' in message:
            for record in message['Records']:
                if 'eventName' in record and record['eventName'].startswith(
                        'ObjectCreated:'
                ):
                    url = 'https://s3.amazonaws.com/{}/{}'.format(
                        record['s3']['bucket']['name'],
                        record['s3']['object']['key']
                    )
                    app.logger.info(
                        '%s: putting url on the queue: %s',
                        record['eventName'], url
                    )
                    q.put(Task(url, datetime.datetime.now(), 0))
                else:
                    app.logger.warning(
                        'Ignoring record with eventName %s',
                        record.get('eventName', 'unknown')
                    )
            if not worker_thread or not worker_thread.is_alive():
                worker_thread = threading.Thread(target=worker)
                worker_thread.daemon = True
                worker_thread.start()
        else:
            app.logger.info(
                'Ignored message of type %s: %s',
                event,
                json.dumps(message, indent=2)
            )

    return '', 200

if __name__ == '__main__':
    app.run(host=BIND_HOST, port=BIND_PORT)
