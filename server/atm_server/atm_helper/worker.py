import os
import sys
import time
from datetime import datetime
import argparse
import logging
from io import StringIO
from multiprocessing import Process

import atm
from atm.worker import work as atm_work
from atm.database import Database, db_session, try_with_session
from atm.config import (add_arguments_aws_s3, add_arguments_sql,
                        add_arguments_logging,
                        load_config, initialize_logging)
from atm.constants import RunStatus

from atm_server.error import ApiError
from atm_server.db import get_db
from atm_server.cache import get_cache


logger = logging.getLogger('atm_server')
logger.setLevel(logging.INFO)


def return_stdout_stderr(f):
    """
    A decorator that stores the stdout and stderr during a function run.
    :param f: a function
    :return: a tuple of (stdout_str, stderr_str, return_of_f)
    """
    def inner(*args, **kwargs):
        stdout_p = StringIO()
        stderr_p = StringIO()
        sys.stdout = stdout_p
        sys.stderr = stderr_p

        try:
            ret = f(*args, **kwargs)
        except:
            ret = None

        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        stdout = stdout_p.getvalue()
        stderr = stderr_p.getvalue()
        stdout_p.close()
        stderr_p.close()
        return stdout, stderr, ret

    return inner


@return_stdout_stderr
def work(*args):
    """
    A copy of the code in atm/scripts/worker.py
    A call to this function will start and run a simple worker
    """
    _logger = logging.getLogger('atm_server.worker:work')
    _logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler('logs/works.log')
    _logger.addHandler(fh)
    parser = argparse.ArgumentParser(description='Add more classifiers to database')
    add_arguments_sql(parser)
    add_arguments_aws_s3(parser)
    add_arguments_logging(parser)

    # add worker-specific arguments
    parser.add_argument('--cloud-mode', action='store_true', default=False,
                        help='Whether to run this worker in cloud mode')
    parser.add_argument('--dataruns', help='Only train on dataruns with these ids',
                        nargs='+')
    parser.add_argument('--time', help='Number of seconds to run worker', type=int)
    parser.add_argument('--choose-randomly', action='store_true',
                        help='Choose dataruns to work on randomly (default = sequential order)')
    parser.add_argument('--no-save', dest='save_files', default=True,
                        action='store_const', const=False,
                        help="don't save models and metrics at all")

    # parse arguments and load configuration
    _args = parser.parse_args(args)

    # default logging config is different if initialized from the command line
    if _args.log_config is None:
        _args.log_config = os.path.join(atm.PROJECT_ROOT,
                                        'config/templates/log-script.yaml')

    sql_config, _, aws_config, log_config = load_config(**vars(_args))
    initialize_logging(log_config)

    # let's go
    _logger.warn('Worker started!')
    atm_work(db=Database(**vars(sql_config)),
             datarun_ids=_args.dataruns,
             choose_randomly=_args.choose_randomly,
             save_files=_args.save_files,
             cloud_mode=_args.cloud_mode,
             aws_config=aws_config,
             log_config=log_config,
             total_time=_args.time,
             wait=False)
    _logger.warn('Worker exited.')


def dispatch_worker(datarun_id):
    """
    dispatch a worker process for a single datarun by calling the work function
    :param datarun_id: the id of the datarun
    :return: Return a handle of the process
    """
    args = ['--dataruns', str(datarun_id)]
    p = Process(target=work, args=args)
    p.start()
    return p
    # Do nothing if the db is already running or complete


def monitor_dispatch_worker(datarun_id):
    # The function is a wrapper of the dispatch worker, which tries to terminate the worker process if needed
    # It create another process that checks the cache and determine if we should terminate the worker process
    p_inner = dispatch_worker(datarun_id)
    logger.warning("Worker process (PID: %d) started" % p_inner.pid)
    if p_inner is None:
        logger.error("Cannot create worker process!")
        return
    register_worker_process(p_inner, datarun_id)
    key = datarun_id2key(datarun_id)
    cache = get_cache()
    while True:
        # Check start
        if not p_inner.is_alive():
            # Clean the cache
            logger.warning("Process (PID: %d) exited." % p_inner.pid)
            cache.delete(key)
            db = get_db()
            datarun = db.get_datarun(datarun_id)
            if datarun.status == RunStatus.RUNNING:
                # The datarun is still running
                mark_datarun_pending(db, datarun_id)
            return
        if cache.get(key) is None or cache.get(key) != p_inner.pid:
            while True:
                p_inner.terminate()
                # terminate() only sends the signal, wait a while to check
                time.sleep(0.01)
                if p_inner.is_alive():
                    logger.warning("Failed to terminate process (PID: %d) after 0.01s" % p_inner.pid)
                    logger.warning("Retrying after 1s...")
                    time.sleep(1)
                else:
                    break
            # Since we have break the run, we need to update the datarun status
            # Note that the run may or may not be complete, due to the delay in terminating
            db = get_db()
            datarun = db.get_datarun(datarun_id)
            if datarun.status == RunStatus.RUNNING:
                # The datarun is still running
                mark_datarun_pending(db,datarun_id)
            logger.warning("Process (PID: %d) terminated (exitcode: %d)" % (p_inner.pid, p_inner.exitcode))
            cache.delete(key)
            return
        # Sleep for a while
        time.sleep(0.001)


def start_worker(datarun_id):
    """
    A function similar to dispatch worker, but provides more comprehensive controls of the worker
    :param datarun_id: the id of the datarun
    :return: Return a handle of the process
    """
    db = get_db()
    datarun = db.get_datarun(datarun_id)
    if datarun is None:
        raise ApiError("No datarun found with the given id: %d" % datarun_id, 404)
    if datarun.status != RunStatus.PENDING:
        # Do nothing if the datarun is already running or complete
        return None

    # Create and start the process
    p = Process(target=monitor_dispatch_worker, args=(datarun_id,))
    p.start()
    # Sleep a while
    time.sleep(0.1)
    return


def stop_worker(datarun_id):
    key = datarun_id2key(datarun_id)
    cache = get_cache()
    pid = cache.get(key)

    if pid is not None:
        logger.warning("Terminating the worker process (PID: %d) of datarun %d" % (pid, datarun_id))
        # Then we delete the datarun process_id cache (as a signal of terminating)
        cache.set(key, 'stop')
        while True:
            if cache.has(key):
                time.sleep(1)
            else:
                return True
        # cache.delete(key)
        # return True
    logger.warning("Cannot find corresponding process for datarun %d" % datarun_id)
    db = get_db()
    datarun = db.get_datarun(datarun_id)
    if datarun is None:
        # WARNING: Raise ERROR
        raise ApiError("No datarun found with the given id: %d" % datarun_id, 404)
    if datarun.status == RunStatus.RUNNING:
        # The datarun maybe terminated.
        mark_datarun_pending(db,datarun_id)
    return False


@try_with_session(commit=True)
def mark_datarun_pending(db, datarun_id):
    datarun = db.get_datarun(datarun_id)
    datarun.status = RunStatus.PENDING
    datarun.end_time = datetime.now()


def datarun_id2key(datarun_id):
    return "worker:%d" % datarun_id


def register_worker_process(process, datarun_id):
    key = datarun_id2key(datarun_id)
    cache = get_cache()
    if cache.has(key):
        raise ApiError("There should be only one live working process for one datarun!", 500)
    cache.set(key, process.pid)
    logger.warning("Worker process (PID: %d) for datarun %d registered" % (process.pid, datarun_id))
