"""
Copyright 2019 Cartesi Pte. Ltd.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use
this file except in compliance with the License. You may obtain a copy of the
License at http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed
under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""

from concurrent import futures
from threading import Lock
import signal
import time
import math
import grpc
import sys
import traceback
import argparse
from grpc_reflection.v1alpha import reflection

import manager_low_pb2_grpc
import manager_low_pb2
import manager_high_pb2_grpc
import manager_high_pb2
import cartesi_base_pb2
import utils
from session_registry import SessionIdException, AddressException, RollbackException

# docker graceful shutdown, raise a KeyboardInterrupt in case of SIGTERM
def handle_sigterm(*args):
    raise KeyboardInterrupt()

signal.signal(signal.SIGTERM, handle_sigterm)

LOGGER = utils.get_new_logger(__name__)
LOGGER = utils.configure_log(LOGGER)

LISTENING_ADDRESS = 'localhost'
LISTENING_PORT = 50051
SLEEP_TIME = 5

class NotReadyException(Exception):
    pass

class SessionJob:

    def __init__(self, session_id):
        self.id = session_id
        self.lock = Lock()
        self.job_hash = None
        self.job_future = None

class _MachineManagerHigh(manager_high_pb2_grpc.MachineManagerHighServicer):

    def __init__(self, session_registry_manager):
        self.session_registry_manager = session_registry_manager
        self.global_lock = Lock()
        self.job = {}

    def __set_job_hash__(self, session_id, request):
        self.job[session_id].job_hash = request

    def __reset_job__(self, session_id):
        self.job[session_id].job_future = None
        self.job[session_id].job_hash = None

    def __try_job__(self, session_id, request, err_msg, fn, *args):
        LOGGER.debug("Acquiring manager global lock")
        with self.global_lock:
            LOGGER.debug("Lock acquired")
            if session_id in self.job.keys():
                with self.job[session_id].lock:
                    if self.job[session_id].job_future is None:
                        with futures.ThreadPoolExecutor() as executor:
                            #Submit job and store the future
                            self.__set_job_hash__(session_id, request)
                            self.job[session_id].job_future = executor.submit(fn, *args)
                            raise NotReadyException(err_msg)
                    elif self.job[session_id].job_future.done():
                        #Check if the job_hash matches
                        if request == self.job[session_id].job_hash:
                            job = self.job[session_id].job_future
                            self.__reset_job__(session_id)
                            return job
                        else:
                            self.__reset_job__(session_id)
                            raise NotReadyException(err_msg)
                    else:
                        raise NotReadyException(err_msg)
            else:
                self.job[session_id] = SessionJob(session_id)
                with self.job[session_id].lock:
                    with futures.ThreadPoolExecutor() as executor:
                        #Submit job and store the future
                        self.__set_job_hash__(session_id, request)
                        self.job[session_id].job_future = executor.submit(fn, *args)
                        raise NotReadyException(err_msg)
        

    def ServerShuttingDown(self, context):
        if self.session_registry_manager.shutting_down:
            context.set_details("Server is shutting down, not accepting new requests")
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            return True
        else:
            return False

    def NewSession(self, request, context):
        try:
            if self.ServerShuttingDown(context):
                return

            session_id = request.session_id
            machine_req = request.machine
            LOGGER.info("New session requested with session_id: {}".format(session_id))
            
            err_msg = "Result is not yet ready for NewSession: " + session_id
            job = self.__try_job__(session_id, request, err_msg, self.session_registry_manager.new_session, session_id, machine_req)
            return job.result()

        #No session with provided id or address issue
        except (SessionIdException, AddressException) as e:
            LOGGER.error(e)
            context.set_details("{}".format(e))
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
        #Generic error catch
        except Exception as e:
            LOGGER.error("An exception occurred: {}\nTraceback: {}".format(e, traceback.format_exc()))
            context.set_details('An exception with message "{}" was raised!'.format(e))
            context.set_code(grpc.StatusCode.UNKNOWN)

    def SessionRun(self, request, context):
        try:
            if self.ServerShuttingDown(context):
                return

            session_id = request.session_id
            final_cycles = request.final_cycles
            LOGGER.info("New session run requested for session_id {} with final cycles {}".format(session_id, final_cycles))

            #Validate cycle values
            utils.validate_cycles(final_cycles)

            err_msg = "Result is not yet ready for SessionRun: " + session_id
            job = self.__try_job__(session_id, request, err_msg, self.session_registry_manager.run_session, session_id, final_cycles)
            return job.result()

        #No session with provided id, address issue, bad final cycles provided or problem during rollback
        except (SessionIdException, AddressException, utils.CycleException, RollbackException) as e:
            LOGGER.error(e)
            context.set_details("{}".format(e))
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
        #Generic error catch
        except Exception as e:
            LOGGER.error("An exception occurred: {}\nTraceback: {}".format(e, traceback.format_exc()))
            context.set_details('An exception with message "{}" was raised!'.format(e))
            context.set_code(grpc.StatusCode.UNKNOWN)

    def SessionStep(self, request, context):
        try:
            if self.ServerShuttingDown(context):
                return

            session_id = request.session_id
            initial_cycle = request.initial_cycle
            LOGGER.info("New session step requested for session_id {} with initial cycle {}".format(session_id, initial_cycle))

            #Validate cycle value
            utils.validate_cycles([initial_cycle])

            err_msg = "Result is not yet ready for SessionStep: " + session_id
            job = self.__try_job__(session_id, request, err_msg, self.session_registry_manager.step_session, session_id, initial_cycle)
            return job.result()

        #No session with provided id, address issue, bad initial cycle provided or problem during rollback
        except (SessionIdException, AddressException, utils.CycleException, RollbackException) as e:
            LOGGER.error(e)
            context.set_details("{}".format(e))
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
        #Generic error catch
        except Exception as e:
            LOGGER.error("An exception occurred: {}\nTraceback: {}".format(e, traceback.format_exc()))
            context.set_details('An exception with message "{}" was raised!'.format(e))
            context.set_code(grpc.StatusCode.UNKNOWN)


    def SessionReadMemory(self, request, context):
        try:
            if self.ServerShuttingDown(context):
                return

            session_id = request.session_id
            read_mem_req = request.position
            cycle = request.cycle
            LOGGER.info("New session memory read requested for session_id {} on cycle {} for address {} with length {}".format(session_id, cycle, read_mem_req.address, read_mem_req.length))

            err_msg = "Result is not yet ready for SessionReadMemory: " + session_id
            job = self.__try_job__(session_id, request, err_msg, self.session_registry_manager.session_read_mem, session_id, cycle, read_mem_req)
            return job.result()

        #No session with provided id or address issue
        except (SessionIdException, AddressException) as e:
            LOGGER.error(e)
            context.set_details("{}".format(e))
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
        #Generic error catch
        except Exception as e:
            LOGGER.error("An exception occurred: {}\nTraceback: {}".format(e, traceback.format_exc()))
            context.set_details('An exception with message "{}" was raised!'.format(e))
            context.set_code(grpc.StatusCode.UNKNOWN)

    def SessionWriteMemory(self, request, context):
        try:
            if self.ServerShuttingDown(context):
                return

            session_id = request.session_id
            write_mem_req = request.position
            cycle = request.cycle
            LOGGER.info("New session memory write requested for session_id {} on cycle {} for address {} with data {}".format(session_id, cycle, write_mem_req.address, write_mem_req.data))

            err_msg = "Result is not yet ready for SessionWriteMemory: " + session_id
            job = self.__try_job__(session_id, request, err_msg, self.session_registry_manager.session_write_mem, session_id, cycle, write_mem_req)
            return job.result()

        #No session with provided id or address issue
        except (SessionIdException, AddressException) as e:
            LOGGER.error(e)
            context.set_details("{}".format(e))
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
        #Generic error catch
        except Exception as e:
            LOGGER.error("An exception occurred: {}\nTraceback: {}".format(e, traceback.format_exc()))
            context.set_details('An exception with message "{}" was raised!'.format(e))
            context.set_code(grpc.StatusCode.UNKNOWN)

    def SessionGetProof(self, request, context):
        try:
            if self.ServerShuttingDown(context):
                return

            session_id = request.session_id
            proof_req = request.target
            cycle = request.cycle

            LOGGER.info("New session proof requested for session_id {} on cycle {} for address {} with log2_size {}".format(session_id, cycle, proof_req.address, proof_req.log2_size))

            err_msg = "Result is not yet ready for SessionWriteMemory: " + session_id
            job = self.__try_job__(session_id, request, err_msg, self.session_registry_manager.session_get_proof, session_id, cycle, proof_req)
            return job.result()

        #No session with provided id or address issue
        except (SessionIdException, AddressException) as e:
            LOGGER.error(e)
            context.set_details("{}".format(e))
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
        #Generic error catch
        except Exception as e:
            LOGGER.error("An exception occurred: {}\nTraceback: {}".format(e, traceback.format_exc()))
            context.set_details('An exception with message "{}" was raised!'.format(e))
            context.set_code(grpc.StatusCode.UNKNOWN)



class _MachineManagerLow(manager_low_pb2_grpc.MachineManagerLowServicer):

    def __init__(self, session_registry_manager):
        self.session_registry_manager = session_registry_manager

    def CommunicateAddress (self, request, context):
        try:
            address = request.address
            session_id = request.session_id

            LOGGER.info("Received a CommunicateAddress request for session_id {} and address {}".format(session_id, address))

            self.session_registry_manager.register_address_for_session(session_id, address)

            #Returning
            return cartesi_base_pb2.Void()

        #No session with provided id
        except SessionIdException as e:
            LOGGER.error(e)
            context.set_details("{}".format(e))
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
        #Generic error catch
        except Exception as e:
            LOGGER.error("An exception occurred: {}\nTraceback: {}".format(e, traceback.format_exc()))
            context.set_details('An exception with message "{}" was raised!'.format(e))
            context.set_code(grpc.StatusCode.UNKNOWN)

def serve(args):
    listening_add = args.address
    listening_port = args.port

    #Importing the defective session registry if defective flag is set
    if args.defective:
        from defective_session_registry import SessionRegistryManager
    else:
        from session_registry import SessionRegistryManager

    manager_address = '{}:{}'.format(listening_add, listening_port)
    session_registry_manager = SessionRegistryManager(manager_address)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    manager_high_pb2_grpc.add_MachineManagerHighServicer_to_server(_MachineManagerHigh(session_registry_manager),
                                                      server)
    manager_low_pb2_grpc.add_MachineManagerLowServicer_to_server(_MachineManagerLow(session_registry_manager),
                                                      server)

    SERVICE_NAMES = (
        manager_high_pb2.DESCRIPTOR.services_by_name['MachineManagerHigh'].full_name,
        manager_low_pb2.DESCRIPTOR.services_by_name['MachineManagerLow'].full_name,
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(SERVICE_NAMES, server)
    server.add_insecure_port(manager_address)
    server.start()
    LOGGER.info("Server started, listening on address {} and port {}".format(listening_add, listening_port))
    try:
        while True:
            time.sleep(SLEEP_TIME)
    except KeyboardInterrupt:
        LOGGER.info("\nIssued to shut down")

        LOGGER.debug("Acquiring session registry global lock")
        #Acquiring lock to write on session registry
        with session_registry_manager.global_lock:
            LOGGER.debug("Session registry global lock acquired")
            session_registry_manager.shutting_down = True

        #Shutdown all active sessions servers
        for session_id in session_registry_manager.registry.keys():
            LOGGER.debug("Acquiring lock for session {}".format(session_id))
            with session_registry_manager.registry[session_id].lock:
                LOGGER.debug("Lock for session {} acquired".format(session_id))
                if (session_registry_manager.registry[session_id].address):
                    utils.shutdown_cartesi_machine_server(session_id, session_registry_manager.registry[session_id].address)

        shutdown_event = server.stop(0)

        LOGGER.info("Waiting for server to stop")
        shutdown_event.wait()
        LOGGER.info("Server stopped")

if __name__ == '__main__':

    #Adding argument parser
    description = "Instantiates a core manager server, responsible for managing and interacting with multiple cartesi machine instances"

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        '--address', '-a',
        dest='address',
        default=LISTENING_ADDRESS,
        help='Address to listen (default: {})'.format(LISTENING_ADDRESS)
    )
    parser.add_argument(
        '--port', '-p',
        dest='port',
        default=LISTENING_PORT,
        help='Port to listen (default: {})'.format(LISTENING_PORT)
    )
    parser.add_argument(
        '--defective', '-d',
        dest='defective',
        action='store_true',
        help='Makes server behave improperly, injecting errors silently in the issued commands\n\n' + '-'*23 + 'WARNING!' + '-'*23 + 'FOR TESTING PURPOSES ONLY!!!\n' + 54*'-'
    )

    #Getting arguments
    args = parser.parse_args()

    serve(args)
