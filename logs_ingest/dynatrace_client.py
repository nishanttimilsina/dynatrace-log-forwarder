#   Copyright 2021 Dynatrace LLC
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import json
import os
import ssl
import time
import urllib
from typing import List, Dict, Tuple
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request

from . import logging
from logs_ingest.self_monitoring import SelfMonitoring, DynatraceConnectivity
from logs_ingest.utils import get_int_environment_value

should_verify_ssl_certificate = os.environ.get("REQUIRE_VALID_CERTIFICATE", "True") in ["True", "true"]
ssl_context = ssl.create_default_context()
if not should_verify_ssl_certificate:
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE


def send_logs(dynatrace_url: str, dynatrace_token: str, logs: List[Dict], self_monitoring: SelfMonitoring):
    # pylint: disable=R0912
    start_time = time.time()
    log_ingest_url = urlparse(dynatrace_url + "/api/v2/logs/ingest").geturl()
    batches = prepare_serialized_batches(logs)

    number_of_http_errors = 0
    for batch in batches:
        try:
            encoded_body_bytes = batch.encode("UTF-8")
            logging.info('Log ingest payload size: {} kB'.format(round((len(encoded_body_bytes) / 1024), 3)))

            self_monitoring.all_requests += 1
            status, reason, response = _perform_http_request(
                method="POST",
                url=log_ingest_url,
                encoded_body_bytes=encoded_body_bytes,
                headers={
                    "Authorization": f"Api-Token {dynatrace_token}",
                    "Content-Type": "application/json; charset=utf-8"
                }
            )
            if status > 299:
                logging.error(f'Log ingest error: {status}, reason: {reason}, url: {log_ingest_url}, body: "{response}"')
                if status == 400:
                    self_monitoring.dynatrace_connectivities.append(DynatraceConnectivity.InvalidInput)
                elif status == 401:
                    self_monitoring.dynatrace_connectivities.append(DynatraceConnectivity.ExpiredToken)
                elif status == 403:
                    self_monitoring.dynatrace_connectivities.append(DynatraceConnectivity.WrongToken)
                elif status in (404, 405):
                    self_monitoring.dynatrace_connectivities.append(DynatraceConnectivity.WrongURL)
                elif status in (413, 429):
                    self_monitoring.dynatrace_connectivities.append(DynatraceConnectivity.TooManyRequests)
                    raise HTTPError(log_ingest_url, 429, "Dynatrace throttling response", "", "")
                elif status == 500:
                    self_monitoring.dynatrace_connectivities.append(DynatraceConnectivity.Other)
                    raise HTTPError(log_ingest_url, 500, "Dynatrace server error", "", "")
            else:
                self_monitoring.dynatrace_connectivities.append(DynatraceConnectivity.Ok)
                logging.info("Log ingest payload pushed successfully")
        except HTTPError as e:
            raise e
        except Exception as e:
            logging.exception("Failed to ingest logs")
            self_monitoring.dynatrace_connectivities.append(DynatraceConnectivity.Other)
            number_of_http_errors += 1
            # all http requests failed and this is the last batch, raise this exception to trigger retry
            if number_of_http_errors == len(batches):
                raise e
        finally:
            self_monitoring.sending_time = time.time() - start_time


def _perform_http_request(
        method: str,
        url: str,
        encoded_body_bytes: bytes,
        headers: Dict
) -> Tuple[int, str, str]:
    req = Request(
        url,
        encoded_body_bytes,
        headers,
        method=method
    )
    try:
        response = urllib.request.urlopen(req, context=ssl_context)
        return response.code, response.reason, response.read().decode("utf-8")
    except HTTPError as e:
        response_body = e.read().decode("utf-8")
        return e.code, e.reason, response_body


# Heavily based on AWS log forwarder batching implementation
def prepare_serialized_batches(logs: List[Dict]) -> List[str]:
    request_body_max_size = get_int_environment_value("DYNATRACE_LOG_INGEST_REQUEST_MAX_SIZE", 1048576)
    log_entry_max_size = request_body_max_size - 2  # account for braces

    batches: List[str] = []

    logs_for_next_batch: List[str] = []
    logs_for_next_batch_total_len = 0

    for log_entry in logs:
        brackets_len = 2
        commas_len = len(logs_for_next_batch) - 1

        new_batch_len = logs_for_next_batch_total_len + brackets_len + commas_len

        next_entry_serialized = json.dumps(log_entry)

        next_entry_size = len(next_entry_serialized.encode("UTF-8"))
        if next_entry_size > log_entry_max_size:
            # shouldn't happen as we are already truncating the content field, but just for safety
            logging.info(f"Dropping entry, as it's size is {next_entry_size}, bigger than max entry size: {log_entry_max_size}")

        batch_length_if_added_entry = new_batch_len + 1 + len(next_entry_serialized)  # +1 is for comma

        if batch_length_if_added_entry > request_body_max_size:
            # would overflow limit, close batch and prepare new
            batch = "[" + ",".join(logs_for_next_batch) + "]"
            batches.append(batch)

            logs_for_next_batch = []
            logs_for_next_batch_total_len = 0

        logs_for_next_batch.append(next_entry_serialized)
        logs_for_next_batch_total_len += next_entry_size

    if len(logs_for_next_batch) >= 1:
        # finalize last batch
        batch = "[" + ",".join(logs_for_next_batch) + "]"
        batches.append(batch)

    return batches