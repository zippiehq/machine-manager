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

from threading import Lock, Condition
import subprocess
import time
import utils

import cartesi_machine_pb2

LOGGER = utils.get_new_logger(__name__)
LOGGER = utils.configure_log(LOGGER)
CHECKIN_WAIT_TIMEOUT_SECONDS = 5.0

class AddressException(Exception):
    pass

class SessionIdException(Exception):
    pass

class RollbackException(Exception):
    pass

class CheckinException(Exception):
    pass

class SessionKillException(Exception):
    pass

class SessionRegistryManager:

    def __init__(self, server_address, checkin_address):
        self.global_lock = Lock()
        self.registry = {}
        self.shutting_down = False
        self.server_address = server_address
        self.checkin_address = checkin_address

    def _wait_for_checkin(self, session_id, error_msg):
        if not self.registry[session_id].checkin_cond.wait(CHECKIN_WAIT_TIMEOUT_SECONDS):
            self._remove_session(session_id)
            raise CheckinException(error_msg)

    def _remove_session(self, session_id):
        LOGGER.debug("Acquiring session registry global lock")
        with self.global_lock:
            LOGGER.debug("Lock acquired")
            self.kill_session(session_id)
            LOGGER.debug("Removing session '{}' from registry".format(session_id))
            del self.registry[session_id]
            LOGGER.debug("Session '{}' removed from registry".format(session_id))

    def kill_session(self, session_id):
        LOGGER.debug("Killing cartesi machine servers for session `{}`".format(session_id))
        cmd_line = ["pkill", "-f", "remote-cartesi-machine.*{}".format(session_id)]
        try:
            proc = subprocess.Popen(cmd_line)
            proc.wait()
        except Exception as e:
            err_msg = "Unable to kill cartesi machine servers for session `{}`: {}".format(session_id, e)
            raise SessionKillException(err_msg)

    def new_session(self, session_id, machine_req, force=False):
        #Checking if force is enabled
        if (force):
            LOGGER.debug("Force is enabled for creating new session with id {}".format(session_id))
            #Checking if there is already a session with the given id
            if (session_id in self.registry.keys()):
                #Shutting down old server if any
                if (self.registry[session_id].address):
                    LOGGER.debug("Shutting down current cartesi machine server for session {}".format(session_id))
                    utils.shutdown_cartesi_machine_server(session_id, self.registry[session_id].address)
                    LOGGER.debug("Cartesi machine server for session {} was shut".format(session_id))

        #Registering new session
        self.register_session(session_id, force)

        LOGGER.debug("Acquiring lock for session {}".format(session_id))
        with self.registry[session_id].lock:
            #Instantiate new cartesi machine server
            self.create_new_cartesi_machine_server(session_id, self.server_address, self.checkin_address)

            #Communication received, create new cartesi machine
            self.create_machine(session_id, machine_req)

            #calculate cartesi machine initial hash
            initial_hash = self.get_machine_root_hash(session_id)

            #Create snapshot
            self.snapshot_machine(session_id)

        LOGGER.debug("Released lock for session {}".format(session_id))

        return initial_hash

    def end_session(self, session_id, silent):
        if (session_id not in self.registry.keys()):
            raise SessionIdException("No session in registry with provided session_id: {}".format(session_id))
        if (not self.registry[session_id].address):
            raise AddressException("Address not set for server with session_id '{}'. Check if machine server was created correctly".format(session_id))

        utils.shutdown_cartesi_machine_server(session_id, self.registry[session_id].address)
        LOGGER.debug("Acquiring session registry global lock")
        with self.global_lock:
            LOGGER.debug("Session registry global lock acquired".format(session_id))
            del self.registry[session_id]
            LOGGER.debug("Session {} removed from registry".format(session_id))
        return cartesi_machine_pb2.Void()

    def run_session(self, session_id, final_cycles):
        summaries = []
        hashes = []
        desired_cycles = [c for c in final_cycles]

        if (session_id not in self.registry.keys()):
            raise SessionIdException("No session in registry with provided session_id: {}".format(session_id))
        if (not self.registry[session_id].address):
            raise AddressException("Address not set for server with session_id '{}'. Check if machine server was created correctly".format(session_id))

        LOGGER.debug("Acquiring lock for session {}".format(session_id))
        with self.registry[session_id].lock:

            #Running up to the first cycle and making a snapshot
            first_c = desired_cycles.pop(0)
            summaries.append(self.run_machine_to_desired_cyle(session_id, first_c))
            self.snapshot_machine(session_id)

            #Getting hash
            hashes.append(self.get_machine_root_hash(session_id))

            #Executing additional runs for given final_cycles
            for c in desired_cycles:
                summaries.append(self.run_and_update_registry_cycle(session_id, c))
                hashes.append(self.get_machine_root_hash(session_id))

        run_result = utils.make_session_run_result(summaries, hashes)

        #Checking if log level is DEBUG or more detailed since building the
        #debug info is expensive
        if LOGGER.getEffectiveLevel() <= utils.logging.DEBUG:
            LOGGER.debug(utils.dump_run_response_to_json(run_result))

        #Returning SessionRunResult
        return run_result

    def step_session(self, session_id, initial_cycle, step_params):
        if (session_id not in self.registry.keys()):
            raise SessionIdException("No session in registry with provided session_id: {}".format(session_id))
        if (not self.registry[session_id].address):
            raise AddressException("Address not set for server with session_id '{}'. Check if machine server was created correctly".format(session_id))

        step_result = None

        LOGGER.debug("Acquiring lock for session {}".format(session_id))
        with self.registry[session_id].lock:

            #First, in case the machine cycle is not the desired step initial cycle, we must put the machine in desired
            #step initial cycle so we can then step and retrieve the access log of that specific cycle
            if (self.registry[session_id].cycle != initial_cycle):
                #It is different, putting machine in initial_cycle
                self.run_machine_to_desired_cyle(session_id, initial_cycle)

            #The machine is in initial_cycle, stepping now
            step_result =  utils.make_session_step_result(self.step_and_update_registry_cycle(session_id, step_params))

        #Checking if log level is DEBUG or more detailed since building the
        #debug info is expensive
        if LOGGER.getEffectiveLevel() <= utils.logging.DEBUG:
            LOGGER.debug(utils.dump_step_response_to_json(step_result))

        #Returning SessionStepResult
        return step_result

    def session_store(self, session_id, store_req):
        if (session_id not in self.registry.keys()):
            raise SessionIdException("No session in registry with provided session_id: {}".format(session_id))
        if (not self.registry[session_id].address):
            raise AddressException("Address not set for server with session_id '{}'. Check if machine server was created correctly".format(session_id))

        LOGGER.debug("Acquiring lock for session {}".format(session_id))

        store_result = None

        with self.registry[session_id].lock:

            #Request to store the cartesi machine
            store_result =  utils.store_machine(session_id, self.registry[session_id].address, store_req)

        #Returning CartesiMachine Void
        return store_result

    def session_read_mem(self, session_id, cycle, read_mem_req):
        if (session_id not in self.registry.keys()):
            raise SessionIdException("No session in registry with provided session_id: {}".format(session_id))
        if (not self.registry[session_id].address):
            raise AddressException("Address not set for server with session_id '{}'. Check if machine server was created correctly".format(session_id))

        read_result = None

        LOGGER.debug("Acquiring lock for session {}".format(session_id))
        with self.registry[session_id].lock:
            #If the machine is not in the desired cycle, put it in the desired cycle
            if (self.registry[session_id].cycle != cycle):
                #It is different, putting machine in the desired cycle
                self.run_machine_to_desired_cyle(session_id, cycle)

            #Read desired memory position
            read_result =  utils.make_session_read_memory_result(utils.read_machine_memory(session_id, self.registry[session_id].address, read_mem_req))

        #Checking if log level is DEBUG or more detailed since building the
        #debug info is expensive
        if LOGGER.getEffectiveLevel() <= utils.logging.DEBUG:
            LOGGER.debug(utils.dump_read_mem_response_to_json(read_result))

        #Returning SessionReadMemoryResult
        return read_result

    def session_write_mem(self, session_id, cycle, write_mem_req):
        if (session_id not in self.registry.keys()):
            raise SessionIdException("No session in registry with provided session_id: {}".format(session_id))
        if (not self.registry[session_id].address):
            raise AddressException("Address not set for server with session_id '{}'. Check if machine server was created correctly".format(session_id))

        write_result = None

        LOGGER.debug("Acquiring lock for session {}".format(session_id))
        with self.registry[session_id].lock:
            #If the machine is not in the desired cycle, put it in the desired cycle
            if (self.registry[session_id].cycle != cycle):
                #It is different, putting machine in the desired cycle
                self.run_machine_to_desired_cyle(session_id, cycle)

            #Write to desired memory position
            write_result =  utils.write_machine_memory(session_id, self.registry[session_id].address, write_mem_req)

        #Checking if log level is DEBUG or more detailed since building the
        #debug info is expensive
        if LOGGER.getEffectiveLevel() <= utils.logging.DEBUG:
            LOGGER.debug(utils.dump_write_mem_response_to_json(write_result))

        #Returning CartesiMachine Void
        return write_result

    def session_get_proof(self, session_id, cycle, proof_req):
        if (session_id not in self.registry.keys()):
            raise SessionIdException("No session in registry with provided session_id: {}".format(session_id))
        if (not self.registry[session_id].address):
            raise AddressException("Address not set for server with session_id '{}'. Check if machine server was created correctly".format(session_id))

        proof_result = None

        LOGGER.debug("Acquiring lock for session {}".format(session_id))

        with self.registry[session_id].lock:
            #If the machine is not in the desired cycle, put it in the desired cycle
            if (self.registry[session_id].cycle != cycle):
                #It is different, putting machine in the desired cycle
                self.run_machine_to_desired_cyle(session_id, cycle)

            #Getting required proof
            proof_result =  utils.get_machine_proof(session_id, self.registry[session_id].address, proof_req)

        #Checking if log level is DEBUG or more detailed since building the
        #debug info is expensive
        if LOGGER.getEffectiveLevel() <= utils.logging.DEBUG:
            LOGGER.debug(utils.dump_get_proof_response_to_json(proof_result))

        #Returning CartesiMachine Proof
        return proof_result.proof


    """
    Here starts the "internal" API, use the methods bellow taking the right precautions such as holding a lock a session
    """


    def register_session(self, session_id, force=False):
        #Acquiring global lock and releasing it when completed
        LOGGER.debug("Acquiring session registry global lock")
        with self.global_lock:
            LOGGER.debug("Lock acquired")
            if ((session_id in self.registry.keys()) and force == False):
                #Session id already in use
                err_msg = "Trying to register a session with a session_id that already exists: {}".format(session_id)
                LOGGER.error(err_msg)
                raise SessionIdException(err_msg)
            else:
                #Registering new session
                self.registry[session_id] = CartesiSession(session_id)
                LOGGER.info("New session registered: {}".format(session_id))

    def register_address_for_session(self, session_id, address):

        if (session_id not in self.registry.keys()):
            raise SessionIdException("No session in registry with provided session_id '{}'".format(session_id))

        LOGGER.debug("Acquiring session registry global lock")
        #Acquiring lock to write on session registry
        with self.global_lock:
            LOGGER.debug("Session registry global lock acquired")
            #Registering address
            self.registry[session_id].address = address
            LOGGER.debug("Address for session '{}' set to {}".format(session_id, address))

    def create_new_cartesi_machine_server(self, session_id, server_address, checkin_address):
        if (session_id not in self.registry.keys()):
            raise SessionIdException("No session in registry with provided session_id '{}'".format(session_id))
        if (self.registry[session_id].address):
            raise AddressException("Address already set for server with session_id '{}'".format(session_id))

        with self.registry[session_id].checkin_lock:
            LOGGER.debug("Creating new cartesi machine server for session_id '{}'".format(session_id))
            utils.new_cartesi_machine_server(session_id, server_address, checkin_address)
            self.registry[session_id].address = None
            error_msg = "Unable to create machine server for session '{}': no checkin request from new machine".format(session_id)
            while True:
                self._wait_for_checkin(session_id, error_msg)
                if self.registry[session_id].address:
                    LOGGER.debug("Server created for session '{}'".format(session_id))
                    break

    def create_machine(self, session_id, machine_req):
        if (session_id not in self.registry.keys()):
            raise SessionIdException("No session in registry with provided session_id: {}".format(session_id))
        if (not self.registry[session_id].address):
            raise AddressException("Address not set for server with session_id '{}'. Check if machine server was created correctly".format(session_id))

        LOGGER.debug("Issuing server to create a new machine for session '{}'".format(session_id))
        utils.new_machine(session_id, self.registry[session_id].address, machine_req)
        LOGGER.debug("Executed creating a new machine for session '{}'".format(session_id))
        #Acquiring lock to write on session registry
        with self.global_lock:
            LOGGER.debug("Session registry global lock acquired")
            self.registry[session_id].creation_machine_req = machine_req
            LOGGER.debug("Saved on registry machine request used to create session '{}'".format(session_id))

    def get_machine_root_hash(self, session_id):
        if (session_id not in self.registry.keys()):
            raise SessionIdException("No session in registry with provided session_id: {}".format(session_id))
        if (not self.registry[session_id].address):
            raise AddressException("Address not set for server with session_id '{}'. Check if machine server was created correctly".format(session_id))

        LOGGER.debug("Issuing server to get machine root hash for session '{}'".format(session_id))
        root_hash = utils.get_machine_hash(session_id, self.registry[session_id].address)
        LOGGER.debug("Executed getting machine root hash for session '{}': 0x{}".format(session_id, root_hash.data.hex()))
        return root_hash

    def snapshot_machine(self, session_id):
        if (session_id not in self.registry.keys()):
            raise SessionIdException("No session in registry with provided session_id: {}".format(session_id))
        if (not self.registry[session_id].address):
            raise AddressException("Address not set for server with session_id '{}'. Check if machine server was created correctly"
                    .format(session_id))

        with self.registry[session_id].checkin_lock:
            LOGGER.debug("Issuing server to create machine snapshot for session '{}'".format(session_id))
            utils.create_machine_snapshot(session_id, self.registry[session_id].address)
            self.registry[session_id].address = None
            error_msg = "Unable to snapshot machine for session '{}': no checkin request from new machine".format(session_id)
            while True:
                self._wait_for_checkin(session_id, error_msg)
                if self.registry[session_id].address:
                    LOGGER.debug("Executed creating machine snapshot for session '{}'".format(session_id))
                    LOGGER.debug("Acquiring session registry global lock")
                    #Acquiring lock to write on session registry
                    with self.global_lock:
                        LOGGER.debug("Session registry global lock acquired")
                        self.registry[session_id].snapshot_cycle = self.registry[session_id].cycle
                        LOGGER.debug("Updated snapshot cycle of session '{}' to {}"
                                .format(session_id, self.registry[session_id].cycle))
                    break

    def rollback_machine(self, session_id):
        if (session_id not in self.registry.keys()):
            raise SessionIdException("No session in registry with provided session_id: {}".format(session_id))
        if (not self.registry[session_id].address):
            raise AddressException("Address not set for server with session_id '{}'. Check if machine server was created correctly"
                    .format(session_id))
        if (self.registry[session_id].snapshot_cycle == None):
            raise RollbackException("There is no snapshot to rollback to for the cartesi machine with session_id '{}'"
                    .format(session_id))

        with self.registry[session_id].checkin_lock:
            LOGGER.debug("Issuing server to rollback machine for session '{}'".format(session_id))
            utils.rollback_machine(session_id, self.registry[session_id].address)
            self.registry[session_id].address = None
            error_msg = "Unable to rollback machine for session '{}': no checkin request from new machine".format(session_id)
            while True:
                self._wait_for_checkin(session_id, error_msg)
                if self.registry[session_id].address:
                    LOGGER.debug("Executed rollingback machine for session '{}'".format(session_id))
                    LOGGER.debug("Acquiring session registry global lock")
                    #Acquiring lock to write on session registry
                    with self.global_lock:
                        LOGGER.debug("Session registry global lock acquired")
                        self.registry[session_id].cycle = self.registry[session_id].snapshot_cycle
                        self.registry[session_id].snapshot_cycle = None
                        LOGGER.debug("Updated cycle of session '{}' to {} and cleared snapshot cycle"
                                .format(session_id, self.registry[session_id].cycle))
                    break

    def recreate_machine(self, session_id):
        if (session_id not in self.registry.keys()):
            raise SessionIdException("No session in registry with provided session_id: {}".format(session_id))

        # Shutting down old server if any
        if (self.registry[session_id].address):
            utils.shutdown_cartesi_machine_server(session_id, self.registry[session_id].address)

        LOGGER.debug("Acquiring session registry global lock")
        #Acquiring lock to write on session registry
        with self.global_lock:
            LOGGER.debug("Session registry global lock acquired")
            #Cleaning old server session data
            self.registry[session_id].address = None
            self.registry[session_id].cycle = 0
            self.registry[session_id].snapshot_cycle = None

        LOGGER.debug("Cleaned old server session data for session '{}'".format(session_id))

        #Instantiate new cartesi machine server
        self.create_new_cartesi_machine_server(session_id, self.server_address, self.checkin_address)

        #Communication received, create new cartesi machine using saved parameters
        self.create_machine(session_id, self.registry[session_id].creation_machine_req)

    def run_and_update_registry_cycle(self, session_id, c):
        LOGGER.debug("Running session {} up to cycle {}".format(session_id, c))
        if (session_id not in self.registry.keys()):
            raise SessionIdException("No session in registry with provided session_id: {}".format(session_id))
        if (not self.registry[session_id].address):
            raise AddressException("Address not set for server with session_id '{}'. Check if machine server was created correctly".format(session_id))

        #Running cartesi machine
        result = utils.run_machine(session_id, self.registry[session_id], c)

        LOGGER.debug("Executed run of session {} with given target cycle {}".format(session_id, c))

        return result

    def step_and_update_registry_cycle(self, session_id, step_params):
        if (session_id not in self.registry.keys()):
            raise SessionIdException("No session in registry with provided session_id: {}".format(session_id))
        if (not self.registry[session_id].address):
            raise AddressException("Address not set for server with session_id '{}'. Check if machine server was created correctly".format(session_id))

        #Stepping cartesi machine
        result = utils.step_machine(session_id, self.registry[session_id].address, step_params)

        #Updating cartesi session cycle
        #Acquiring lock to write on session registry
        with self.global_lock:
            LOGGER.debug("Session registry global lock acquired")
            self.registry[session_id].cycle += 1

        LOGGER.debug("Updated cycle of session '{}' to {}".format(session_id, self.registry[session_id].cycle))

        return result

    def run_machine_to_desired_cyle(self, session_id, c):
        if (session_id not in self.registry.keys()):
            raise SessionIdException("No session in registry with provided session_id: {}".format(session_id))
        if (not self.registry[session_id].address):
            raise AddressException("Address not set for server with session_id '{}'. Check if machine server was created correctly".format(session_id))

        #Checking machine cycle is after required cycle
        if (self.registry[session_id].cycle > c):
            #It is, checking if there is a snapshot image
            if (self.registry[session_id].snapshot_cycle != None):
                #There is, checking if snapshot cycle is before or after required cycle
                if (self.registry[session_id].snapshot_cycle <= c):
                    #It is, rolling back
                    self.rollback_machine(session_id)
                else:
                    #It isn't, recreating machine from scratch
                    self.recreate_machine(session_id)
            else:
                #There isn't, recreating machine from scratch
                self.recreate_machine(session_id)

        #Execute run and return the result
        return self.run_and_update_registry_cycle(session_id, c)

class CartesiSession:

    def __init__(self, session_id):
        self.id = session_id
        self.lock = Lock()
        self.checkin_lock = Lock()
        self.checkin_cond = Condition(lock=self.checkin_lock)
        self.address = None
        self.cycle = 0
        self.snapshot_cycle = None
        self.creation_machine_req = None
        self.updated_at = time.time()
        self.app_progress = 0
        self.halt_cycle = None
