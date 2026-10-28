#!/usr/bin/env python3
# © Broadcom. All Rights Reserved.
# The term "Broadcom" refers to Broadcom Inc. and/or its subsidiaries.
# SPDX-License-Identifier: Apache-2.0
"""Disposable RabbitMQ worker: publish confirmed results before acknowledging work."""

import hashlib
import json
import logging
import os
import subprocess
import time

import pika

def process(connection, channel, method, properties, body):
    """Hashing the job ID is idempotent; repeated result records carry the same ID."""
    try:
        job = json.loads(body)
        job_id = job["id"]
        seconds = float(job.get("seconds", 10))
    except (ValueError, KeyError, TypeError, AttributeError):
        # A malformed job can never succeed. Reject it without requeueing so it
        # is dropped instead of redelivering forever and blocking prefetch 1.
        logging.warning("rejecting malformed job")
        channel.basic_reject(method.delivery_tag, requeue=False)
        return
    # Keep AMQP heartbeats alive while simulating work. A killed VM leaves this
    # delivery unacknowledged, so RabbitMQ redelivers it to another worker.
    connection.sleep(seconds)
    result = {"id": job_id, "sha256": hashlib.sha256(str(job_id).encode()).hexdigest(),
              "worker": os.uname().nodename, "redelivered": method.redelivered}
    channel.basic_publish("", os.environ["RESULT_QUEUE"], json.dumps(result).encode(),
                          pika.BasicProperties(delivery_mode=2), mandatory=True)
    channel.basic_ack(method.delivery_tag)
    logging.info("completed %s", job_id)


def main():
    logging.basicConfig(level=logging.INFO)
    while True:
        subprocess.run(["vmware-rpctool", "info-set guestinfo.vmrs-ready false"], check=True)
        connection = None
        try:
            parameters = pika.URLParameters(os.environ["BROKER_AMQP_URL"])
            parameters.heartbeat = 30
            parameters.blocked_connection_timeout = 30
            connection = pika.BlockingConnection(parameters)
            channel = connection.channel()
            channel.queue_declare(os.environ["WORK_QUEUE"], durable=True)
            channel.queue_declare(os.environ["RESULT_QUEUE"], durable=True)
            channel.basic_qos(prefetch_count=1)
            channel.confirm_delivery()
            channel.basic_consume(os.environ["WORK_QUEUE"],
                                  lambda ch, method, props, body: process(connection, ch, method, props, body))
            subprocess.run(["vmware-rpctool", "info-set guestinfo.vmrs-ready true"], check=True)
            channel.start_consuming()
        except (pika.exceptions.AMQPError, OSError, ValueError, KeyError):
            # Do not log exception strings, which can include credentials.
            logging.warning("worker disconnected or received invalid work; reconnecting")
        finally:
            subprocess.run(["vmware-rpctool", "info-set guestinfo.vmrs-ready false"], check=False)
            if connection and connection.is_open:
                connection.close()
        time.sleep(5)


if __name__ == "__main__":
    main()
