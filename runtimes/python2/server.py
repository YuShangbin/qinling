# Copyright 2017 Catalyst IT Limited
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import importlib
import json
from multiprocessing import Manager
from multiprocessing import Process
import os
import resource
import sys
import time
import traceback

from flask import Flask
from flask import request
from flask import Response
from keystoneauth1.identity import generic
from keystoneauth1 import session
import requests

app = Flask(__name__)

DOWNLOAD_ERROR = "Failed to download function package from %s, error: %s"
INVOKE_ERROR = "Function execution failed because of too much resource " \
               "consumption"


def _print_trace():
    exc_type, exc_value, exc_traceback = sys.exc_info()
    lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
    print(''.join(line for line in lines))


def _set_ulimit():
    """Limit resources usage for the current process and/or its children.

    Refer to https://docs.python.org/2.7/library/resource.html
    """
    customized_limits = {
        resource.RLIMIT_NOFILE: 1024,
        resource.RLIMIT_NPROC: 128,
        resource.RLIMIT_FSIZE: 61440
    }
    for t, soft in customized_limits.items():
        _, hard = resource.getrlimit(t)
        resource.setrlimit(t, (soft, hard))


def _get_responce(output, duration, logs, success, code):
    return Response(
        response=json.dumps(
            {
                'output': output,
                'duration': duration,
                'logs': logs,
                'success': success
            }
        ),
        status=code,
        mimetype='application/json'
    )


def _invoke_function(execution_id, zip_file_dir, module_name, method, arg,
                     input, return_dict, rlimit):
    """Thie function is supposed to be running in a child process.

    HOSTNAME will be used to create cgroup directory related to worker.

    Current execution pid will be added to cgroup tasks file, and then all
    its child processes will be automatically added to this 'cgroup'.

    Once executions exceed the cgroup limit, they will be killed by OOMKill
    and this subprocess will exit with number(-9).
    """
    # Set resource limit for current sub-process
    _set_ulimit()

    # Set cpu and memory limits to cgroup by calling cglimit service
    pid = os.getpid()
    root_resp = requests.post(
        'http://localhost:9092/cglimit',
        json={
            'cpu': rlimit['cpu'],
            'memory_size': rlimit['memory_size'],
            'pid': pid
        }
    )
    if not root_resp.ok:
        return_dict['result'] = root_resp.content
        return_dict['success'] = False
        sys.exit(0)

    sys.path.insert(0, zip_file_dir)
    sys.stdout = open("%s.out" % execution_id, "w", 0)

    print('Start execution: %s' % execution_id)

    try:
        module = importlib.import_module(module_name)
        func = getattr(module, method)
        return_dict['result'] = func(arg, **input) if arg else func(**input)
        return_dict['success'] = True
    except Exception as e:
        _print_trace()

        if isinstance(e, OSError) and 'Resource' in str(e):
            sys.exit(1)

        return_dict['result'] = str(e)
        return_dict['success'] = False
    finally:
        print('Finished execution: %s' % execution_id)


@app.route('/execute', methods=['POST'])
def execute():
    """Invoke function.

    Several things need to handle in this function:
    - Save the function log
    - Capture the function internal exception
    - Deal with process execution error (The process may be killed for some
      reason, e.g. unlimited memory allocation)
    - Deal with os error for process (e.g. Resource temporarily unavailable)
    """
    params = request.get_json() or {}
    input = params.get('input') or {}
    execution_id = params['execution_id']
    download_url = params.get('download_url')
    function_id = params.get('function_id')
    entry = params.get('entry')
    request_id = params.get('request_id')
    trust_id = params.get('trust_id')
    auth_url = params.get('auth_url')
    username = params.get('username')
    password = params.get('password')
    zip_file_dir = '/var/qinling/packages/%s' % function_id
    rlimit = {
        'cpu': params['cpu'],
        'memory_size': params['memory_size']
    }

    function_module, function_method = 'main', 'main'
    if entry:
        function_module, function_method = tuple(entry.rsplit('.', 1))

    print(
        'Request received, request_id: %s, execution_id: %s, input: %s, '
        'auth_url: %s' %
        (request_id, execution_id, input, auth_url)
    )

    ####################################################################
    #
    # Download function package by calling sidecar service. We don't check the
    # zip file existence here to avoid using partial file during downloading.
    #
    ####################################################################
    resp = requests.post(
        'http://localhost:9091/download',
        json={
            'download_url': download_url,
            'function_id': function_id,
            'token': params.get('token')
        }
    )
    if not resp.ok:
        return _get_responce(resp.content, 0, '', False, 500)

    ####################################################################
    #
    # Provide an openstack session to user's function
    #
    ####################################################################
    os_session = None
    if auth_url:
        auth = generic.Password(
            username=username,
            password=password,
            auth_url=auth_url,
            trust_id=trust_id,
            user_domain_name='Default'
        )
        os_session = session.Session(auth=auth, verify=False)
    input.update({'context': {'os_session': os_session}})

    ####################################################################
    #
    # Create a new process to run user's function
    #
    ####################################################################
    manager = Manager()
    return_dict = manager.dict()
    return_dict['success'] = False
    start = time.time()

    # Run the function in a separate process to avoid messing up the log
    p = Process(
        target=_invoke_function,
        args=(execution_id, zip_file_dir, function_module, function_method,
              input.pop('__function_input', None), input, return_dict, rlimit)
    )
    p.start()
    p.join()

    ####################################################################
    #
    # Get execution output(log, duration, etc.)
    #
    ####################################################################
    duration = round(time.time() - start, 3)

    # Process was killed unexpectedly or finished with error.
    if p.exitcode != 0:
        output = INVOKE_ERROR
        success = False
    else:
        output = return_dict.get('result')
        success = return_dict['success']

    # Execution log
    with open('%s.out' % execution_id) as f:
        logs = f.read()
    os.remove('%s.out' % execution_id)

    return _get_responce(output, duration, logs, success, 200)


@app.route('/ping')
def ping():
    return 'pong'
