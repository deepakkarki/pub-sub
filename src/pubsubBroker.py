import threading, queue
import sys, os
import logging
from xmlrpc.server import SimpleXMLRPCServer
from xmlrpc.server import SimpleXMLRPCRequestHandler
from socketserver import ThreadingMixIn
from kazoo.client import KazooClient
from kazoo.client import KazooState
from kazoo.exceptions import KazooException, OperationTimeoutError
from kazoo.protocol.paths import join

from chordNode import create_chord_ring
from event import *
from zk_helpers import *

BROKER_REG_PATH = "/brokerRegistry"

logging.basicConfig(level=logging.WARNING)

class RequestHandler(SimpleXMLRPCRequestHandler):
    rpc_paths = ('/RPC2',)

class threadedXMLRPCServer(ThreadingMixIn, SimpleXMLRPCServer):
    pass


class PubSubBroker:

    def __init__(self, my_address, zk_hosts):
        self.my_znode = ""
        self.my_address = my_address
        self.zk_hosts = zk_hosts
        self.zk_client = KazooClient(hosts=makeHostsString(zk_hosts))
        self.zk_client.add_listener(self.state_change_handler)
        self.brokers = [] # array of ChordNodes representing the ChordRing 

        # Let Broker Control Functionality by responding to events
        self.event_queue = queue.Queue()
        self.operational = False # RPC method should/not accept requests

        # Topic data structures
        # keeping it simple...it's just a single integer instead of a map
        # of topic queues
        self.topic_data = 0 
        self.data_lock = threading.Lock()


    # RPC Methods ==========================
    
    def enqueue(self, topic: str, message: str):
        if self.operational:
            self.data_lock.acquire()
            self.topic_data += 1
            print("Data value: {}".format(str(self.topic_data)))
            self.data_lock.release()
            return True
        else:   
            return False

    def enqueue_replica(self, topic: str, message: str, index: int):
        pass

    def last_index(self, topic: str):
        pass

    def consume(self, topic: str, index: int):
        pass

    # Control Methods ========================

    def serve(self):
        # start process of joining the system
        self.event_queue.put(ControlEvent(EventType.RESTART_BROKER))

        while True: # infinite Broker serving loop
            # Wait for an event off the communication channel
            # and respond to it
            event = self.event_queue.get() # blocking call

            if event.name == EventType.PAUSE_OPER:
                pass
            elif event.name == EventType.RESUME_OPER:
                pass
            elif event.name == EventType.RESTART_BROKER:
                # retry Making connection with ZooKeeper and joining the cluster
                dt = threading.Thread(target=self.join_cluster, daemon=True)
                dt.start()
            elif event.name == EventType.RING_UPDATE:
                ring = event.data[CHORD_RING]
                dt = threading.Thread(target=self.manage_ring_update, args=(ring,), daemon=True)
                dt.start()
                # reset watch on Broker Registry in ZooKeeper
                self.zk_client.get_children(BROKER_REG_PATH, watch=self.build_updated_chord_ring)
            elif event.name == EventType.UPDATE_TOPICS:
                pass
            elif event.name == EventType.VIEW_CHANGE:
                pass
            else:
                logging.warning("Unknown Event detected: {}".format(event.name))
        
    def join_cluster(self):
        try:
            # start the client
            self.zk_client.start()
            
            # create a watch and a new node for this broker
            self.zk_client.ensure_path(BROKER_REG_PATH)
            self.zk_client.get_children(BROKER_REG_PATH, watch=self.build_updated_chord_ring)
            my_path = BROKER_REG_PATH + "/{}".format(self.my_address)
            self.my_znode = self.zk_client.create(my_path, value="true".encode("utf-8"), ephemeral=True)

        except Exception as e:
            logging.warning("Join Cluster error: {}".format(e))
            self.event_queue.put(ControlEvent(EventType.RESTART_BROKER))

    def manage_ring_update(self, updated_ring):
        # Print to logs
        formatted = ["{}".format(str(node)) for node in updated_ring]
        logging.warning("Broker Watch: {}".format(", ".join(formatted)))

        # Detect if this broker should do something about this change
        # TODO
        # predecessor_changed = check_if_new_leader(updated_ring, self.brokers, self.my_address)

        # Replace local cached copy with new ring
        self.brokers = updated_ring
        return

    def build_updated_chord_ring(self, watch_event):
        # build updated chord ring
        broker_addrs = self.zk_client.get_children(BROKER_REG_PATH) 
        updated_ring = create_chord_ring(broker_addrs)

        # send event back to Broker controller
        data = {CHORD_RING: updated_ring}
        event = ControlEvent(EventType.RING_UPDATE, data)
        self.event_queue.put(event)
        return

    def state_change_handler(self, conn_state):
        if conn_state == KazooState.LOST:
            logging.warning("Kazoo Client detected a Lost state")
            self.event_queue.put(ControlEvent(EventType.RESTART_BROKER))
        elif conn_state == KazooState.SUSPENDED:
            logging.warning("Kazoo Client detected a Suspended state")
            self.event_queue.put(ControlEvent(EventType.PAUSE_OPER))
        elif conn_state == KazooState.CONNECTED: # KazooState.CONNECTED
            logging.warning("Kazoo Client detected a Connected state")
            self.event_queue.put(ControlEvent(EventType.RESUME_OPER))
        else:
            logging.warning("Kazoo Client detected an UNKNOWN state")

    # def dynamic_watch(watch_information): 
    #     # Check for changes that would imply that the broker should DO SOMETHING
    #     
    #     self.brokers = updated_ring
    #     formatted = ["{}".format(str(node)) for node in updated_ring]
    #     print("Broker Watch: {}".format(", ".join(formatted)))
    #     # print("Responsible for view change: {}".format(str(predecessor_changed))) 



if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python pubsubBroker.py <configuration_path> <zk_config>") 
        exit(1)

    print("Starting PubSub Broker...")

    # Load up the the Broker configuration  
    # TODO: Yml or something would be cool if we feel like it
    my_url = 'localhost:3000' 
    broker_config_path = sys.argv[1]
    zk_config_path = sys.argv[2]

    exists = os.path.isfile(broker_config_path) 
    if exists:
        with open(broker_config_path, "r") as f:
            broker_conf_array = f.readlines()
            my_url = broker_conf_array[0].strip() # Smh

    my_ip_addr = my_url.split(":")[0]
    my_port = int(my_url.split(":")[1])

    # Display the loaded configuration
    print("Address:\t{}".format(my_url))

    # Load up the Supporting Zookeeper Configuration
    zk_hosts = get_zookeeper_hosts(zk_config_path)

    # Create the Broker and Spin up its RPC server
    rpc_server = threadedXMLRPCServer((my_ip_addr, my_port), requestHandler=RequestHandler)
    broker = PubSubBroker(my_url, zk_hosts)

    # Register all functions in the Broker's Public API
    rpc_server.register_introspection_functions()
    rpc_server.register_function(broker.enqueue, "broker.enqueue")
    rpc_server.register_function(broker.enqueue_replica, "broker.enqueue_replica")
    rpc_server.register_function(broker.last_index, "broker.last_index")
    rpc_server.register_function(broker.consume, "broker.consume")

    # Control Broker management
    service_thread = threading.Thread(target=broker.serve) 
    service_thread.start()

    # Start Broker RPC Server
    rpc_server.serve_forever()

    service_thread.join()
