import json
from collections import OrderedDict, defaultdict
from rospkg import RosPack

import rospy
from geometry_msgs.msg import PoseStamped, Point, Quaternion

from json_prolog import json_prolog
from refills_first_review.tfwrapper import TfWrapper

MAP = 'map'
SHOP = 'shop'
SHELF_FLOOR = '{}:\'ShelfLayer\''.format(SHOP)
DM_MARKET = 'dmshop'
SHELF_SYSTEM = '{}:\'DMShelfSystem\''.format(DM_MARKET)
SHELF_METER = '{}:\'DMShelfFrameFrontStore\''.format(DM_MARKET)
SHELF_FLOOR_STANDING = '{}:\'DMShelfLayer4TilesFront\''.format(DM_MARKET)
SHELF_FLOOR_STANDING_GROUND = '{}:\'DMShelfLayer5TilesFront\''.format(DM_MARKET)
SHELF_FLOOR_MOUNTING = '{}:\'DMShelfLayerMountingFront\''.format(DM_MARKET)
SEPARATOR = '{}:\'DMShelfSeparator4Tiles\''.format(DM_MARKET)
MOUNTING_BAR = '{}:\'DMShelfMountingBar\''.format(DM_MARKET)
BARCODE = '{}:\'DMShelfLabel\''.format(DM_MARKET)
PERCEPTION_AFFORDANCE = '{}:\'DMShelfPerceptionAffordance\''.format(DM_MARKET)


class ActionGraph(object):
    def __init__(self, knowrob, parent_node=None, previous_node=None, id=''):
        self.knowrob = knowrob
        self.previous_node = previous_node
        self.parent_node = parent_node
        self.last_sub_action = None
        self.id = id

    @classmethod
    def start_experiment(cls, knowrob, action_type):
        q = 'cram_start_action(\'{}\', \'\', {}, _, R)'.format(action_type, rospy.get_rostime())
        id = knowrob.prolog_query(q)[0]['R']
        return cls(knowrob, id=id)

    def finish(self):
        q = 'cram_finish_action({}, {})'.format(self.id, rospy.get_rostime())
        self.knowrob.prolog_query(q)
        return self.parent_node

    def create_thingy(self, action_type):
        previous_action = self.last_sub_action.id if self.last_sub_action is not None else '_'
        q = 'cram_start_action(\'{}\', \'{}\', {}, {}, R)'.format(action_type, '',
                                                                  rospy.get_rostime(),
                                                                  previous_action)
        return self.knowrob.prolog_query(q)[0]['R']

    def add_sub_thingy(self, action_type, sub_type, object_acted_on=None, goal_location=None, detected_object=None):
        new_id = self.create_thingy(action_type)
        q = 'rdf_assert({}, {}, {}, \'LoggingGraph\')'.format(self.id, sub_type, new_id)
        self.knowrob.prolog_query(q)

        self.last_sub_action = ActionGraph(knowrob=self.knowrob, parent_node=self, previous_node=self.last_sub_action,
                                           id=new_id)
    def add_sub_action(self, action_type, object_acted_on=None, goal_location=None, detected_object=None):
        self.add_sub_thingy(action_type, 'knowrob:subAction', object_acted_on, goal_location, detected_object)

    def add_sub_event(self, event_type, object_acted_on=None, goal_location=None, detected_object=None):
        self.add_sub_thingy(event_type, 'knowrob:subEvent', object_acted_on, goal_location, detected_object)

    def add_sub_motion(self, motion_type, object_acted_on=None, goal_location=None, detected_object=None):
        self.add_sub_thingy(motion_type, 'knowrob:subMotion', object_acted_on, goal_location, detected_object)

    def __str__(self):
        return self.id.split('3')[-1]


class KnowRob(object):
    def __init__(self):
        # TODO implement all the things [high]
        # TODO use paramserver [low]
        self.floors = {}
        self.shelf_ids = []
        self.separators = {}
        self.perceived_frame_id_map = {}
        self.action_graph = None
        self.tf = TfWrapper()
        self.prolog = json_prolog.Prolog()
        self.prolog.wait_for_service()

    def prolog_query(self, q):
        print(q)
        query = self.prolog.query(q)
        solutions = [x if x != {} else True for x in query.solutions()]
        if len(solutions) > 1:
            rospy.logwarn('{} returned more than one result'.format(q))
        elif len(solutions) == 0:
            rospy.logwarn('{} returned nothing'.format(q))
        query.finish()
        return solutions

    def remove_http_shit(self, s):
        return s.split('#')[-1].split('\'')[0]

    def load_barcode_to_mesh_map(self):
        self.barcode_to_mesh = json.load(open('../../data/barcode_to_mesh.json'))

    def pose_to_prolog(self, pose_stamped):
        return '[\'{}\', _, [{},{},{}], [{},{},{},{}]]'.format(pose_stamped.header.frame_id,
                                                               pose_stamped.pose.position.x,
                                                               pose_stamped.pose.position.y,
                                                               pose_stamped.pose.position.z,
                                                               pose_stamped.pose.orientation.x,
                                                               pose_stamped.pose.orientation.y,
                                                               pose_stamped.pose.orientation.z,
                                                               pose_stamped.pose.orientation.w)

    def prolog_to_pose_msg(self, query_result):
        ros_pose = PoseStamped()
        ros_pose.header.frame_id = query_result[0]
        ros_pose.pose.position = Point(*query_result[2])
        ros_pose.pose.orientation = Quaternion(*query_result[3])
        return ros_pose

    def add_shelf_system(self):
        q = 'belief_new_object({}, R)'.format(SHELF_SYSTEM)
        shelf_system_id = self.prolog_query(q)[0]['R'].replace('\'', '')
        return shelf_system_id

    # shelves
    def add_shelves(self, shelf_system_id, shelves):
        # TODO failure handling
        for name, pose in shelves.items():
            q = 'belief_new_object({}, ID), ' \
                'rdf_assert(\'{}\', knowrob:properPhysicalParts, ID),' \
                'object_affordance_static_transform(ID, A, [_,_,T,R]),' \
                'rdfs_individual_of(A, {})'.format(SHELF_METER, shelf_system_id, PERCEPTION_AFFORDANCE)
            solutions = self.prolog_query(q)[0]
            pose.pose.position.x -= solutions['T'][0]
            pose.pose.position.y -= solutions['T'][1]
            pose.pose.position.z -= solutions['T'][2]
            object_id = solutions['ID'].replace('\'', '')
            q = 'belief_at_update(\'{}\', {})'.format(object_id, self.pose_to_prolog(pose))
            solutions = self.prolog_query(q)

        return True

    def get_objects(self, type):
        # TODO failure handling
        objects = OrderedDict()
        q = 'rdfs_individual_of(R, {}).'.format(type)
        solutions = self.prolog_query(q)
        for solution in solutions:
            object_id = solution['R'].replace('\'', '')
            pose_q = 'belief_at(\'{}\', R).'.format(object_id)
            believed_pose = self.prolog_query(pose_q)[0]['R']
            ros_pose = PoseStamped()
            ros_pose.header.frame_id = believed_pose[0]
            ros_pose.pose.position = Point(*believed_pose[2])
            ros_pose.pose.orientation = Quaternion(*believed_pose[3])
            objects[object_id] = ros_pose
        return objects

    def get_shelves(self):
        return self.get_objects(SHELF_METER)

    def get_perceived_frame_id(self, object_id):
        if object_id not in self.perceived_frame_id_map:
            q = 'object_perception_affordance_frame_name(\'{}\', F)'.format(object_id)
            self.perceived_frame_id_map[object_id] = self.prolog_query(q)[0]['F'].replace('\'', '')
        return self.perceived_frame_id_map[object_id]

    def get_object_frame_id(self, object_id):
        q = 'object_frame_name(\'{}\', R).'.format(object_id)
        return self.prolog_query(q)[0]['R'].replace('\'', '')

    # floor
    def add_shelf_floors(self, shelf_id, floors):
        for position in floors:
            if position[1] < 0.13:
                if position[2] < 0.2:
                    layer_type = SHELF_FLOOR_STANDING_GROUND
                else:
                    layer_type = SHELF_FLOOR_STANDING
            else:
                layer_type = SHELF_FLOOR_MOUNTING
            q = 'belief_shelf_part_at(\'{}\', {}, {}, R)'.format(shelf_id, layer_type, position[-1])
            self.prolog_query(q)
        return True

    def get_floor_ids(self, shelf_id):
        q = 'rdf_has(\'{}\', knowrob:properPhysicalParts, Floor), ' \
            'rdfs_individual_of(Floor, {}), ' \
            'object_perception_affordance_frame_name(Floor, Frame).'.format(shelf_id, SHELF_FLOOR)

        solutions = self.prolog_query(q)
        floors = []
        shelf_frame_id = self.get_perceived_frame_id(shelf_id)
        for solution in solutions:
            floor_id = solution['Floor'].replace('\'', '')
            floor_pose = self.tf.lookup_transform(shelf_frame_id, solution['Frame'].replace('\'', ''))
            if floor_pose.pose.position.z < 1.2:
                floors.append((floor_id, floor_pose))
        floors = list(sorted(floors, key=lambda x: x[1].pose.position.z))
        self.floors = OrderedDict(floors)
        return self.floors

    def get_floor_width(self):
        # TODO
        return 1.0

    def get_floor_position(self, floor_id):
        return self.floors[floor_id]

    def is_floor_too_high(self, floor_id):
        return self.get_floor_position(floor_id).pose.position.z > 1.2

    def is_bottom_floor(self, floor_id):
        return self.get_floor_position(floor_id).pose.position.z < 0.16

    def is_hanging_foor(self, floor_id):
        q = 'rdfs_individual_of(\'{}\', {})'.format(floor_id, SHELF_FLOOR_MOUNTING)
        solutions = self.prolog_query(q)
        return len(solutions) > 0

    def is_normal_floor(self, floor_id):
        return not self.is_bottom_floor(floor_id) and not self.is_hanging_foor(floor_id)

    def add_separators(self, floor_id, separators):
        for p in separators:
            q = 'belief_shelf_part_at(\'{}\', {}, {}, _)'.format(floor_id, SEPARATOR, p.pose.position.x)
            self.prolog_query(q)
        return True

    def add_barcodes(self, floor_id, barcodes):
        for barcode, p in barcodes.items():
            q = 'belief_shelf_barcode_at(\'{}\', {}, dan(\'{}\'), {}, _)'.format(floor_id, BARCODE,
                                                                                 barcode, p.pose.position.x)
            self.prolog_query(q)

    def add_separators_and_barcodes(self, floor_id, separators, barcodes):
        separator_q = ','.join(
            ['belief_shelf_part_at(\'{}\', {}, norm({}), _)'.format(floor_id, SEPARATOR, p.pose.position.x)
             for p in separators])

        barcode_q = ','.join(['belief_shelf_barcode_at(\'{}\', {}, dan(\'{}\'), norm({}), _)'.format(floor_id, BARCODE,
                                                                                                     barcode,
                                                                                                     p.pose.position.x)
                              for barcode, p in barcodes.items()])
        self.prolog_query('{},{}'.format(separator_q, barcode_q))

    def add_mounting_bars_and_barcodes(self, floor_id, separators, barcodes):
        separator_q = ','.join(
            ['belief_shelf_part_at(\'{}\', {}, {}, _)'.format(floor_id, MOUNTING_BAR, p.pose.position.x)
             for p in separators])

        barcode_q = ','.join(['belief_shelf_barcode_at(\'{}\', {}, dan(\'{}\'), {}, _)'.format(floor_id, BARCODE,
                                                                                               barcode,
                                                                                               p.pose.position.x)
                              for barcode, p in barcodes.items()])
        self.prolog_query('{},{}'.format(separator_q, barcode_q))

    def get_facings(self, floor_id):
        q = 'findall([F, LF, RF], (shelf_facing(\'{}\', F), ' \
            'rdf_has(F, shop:leftSeparator, L), object_perception_affordance_frame_name(L, LF),' \
            'rdf_has(F, shop:rightSeparator, R), object_perception_affordance_frame_name(R, RF)),' \
            'Facings).'.format(floor_id)
        solutions = self.prolog_query(q)[0]
        facings = {}
        for facing_id, left_separator, right_separator in solutions['Facings']:
            facing_pose = self.tf.lookup_transform(self.get_perceived_frame_id(floor_id),
                                                   self.get_object_frame_id(facing_id))
            facings[facing_id] = (facing_pose, left_separator, right_separator)
        return facings

    def add_object(self, facing_id):
        q = 'product_spawn_front_to_back(\'{}\', ObjId)'.format(facing_id)
        self.prolog_query(q)

    def save_beliefstate(self):
        path = '{}/data/beliefstate.owl'.format(RosPack().get_path('refills_first_review'))
        q = 'rdf_save(\'{}\', belief_state)'.format(path)
        self.prolog_query(q)

    def save_action_graph(self):
        path = '{}/data/action_graph.owl'.format(RosPack().get_path('refills_first_review'))
        q = 'rdf_save(\'{}\', [graph(\'LoggingGraph\')])'.format(path)
        self.prolog_query(q)

    def start_everything(self):
        a = 'muh#experiment'
        self.action_graph = ActionGraph.start_experiment(self, a)

    def start_shelf_system_mapping(self, shelf_system_id):
        a = 'http://knowrob.org/kb/shop.owl#ShelfSystemMapping'
        self.action_graph = self.action_graph.add_sub_action(self, a, object_acted_on=shelf_system_id)

    def start_shelf_frame_mapping(self, shelf_id):
        a = 'http://knowrob.org/kb/shop.owl#ShelfFrameMapping'
        if self.action_graph is not None:
            self.action_graph = self.action_graph.add_sub_action(a, object_acted_on=shelf_id)

    def start_shelf_layer_mapping(self, floor_id):
        a = 'http://knowrob.org/kb/shop.owl#ShelfLayerMapping'
        if self.action_graph is not None:
            self.action_graph = self.action_graph.add_sub_action(a, object_acted_on=floor_id)

    def start_finding_shelf_layer(self):
        a = 'http://knowrob.org/kb/shop.owl#FindingShelfLayer'
        if self.action_graph is not None:
            self.action_graph = self.action_graph.add_sub_action(a)

    def start_finding_shelf_layer_parts(self):
        a = 'http://knowrob.org/kb/shop.owl#FindingShelfLayerParts'
        if self.action_graph is not None:
            self.action_graph = self.action_graph.add_sub_action(a)

    def start_shelf_layer_perception(self):
        a = 'http://knowrob.org/kb/shop.owl#ShelfLayerPerception'
        if self.action_graph is not None:
            self.action_graph = self.action_graph.add_sub_action(a)

    def start_shelf_layer_counting(self):
        a = 'muh#Counting'
        if self.action_graph is not None:
            self.action_graph = self.action_graph.add_sub_action(a)

    def start_move_to_shelf_frame(self):
        a = 'http://knowrob.org/kb/shop.owl#MoveToShelfFrame'
        if self.action_graph is not None:
            self.action_graph = self.action_graph.add_sub_action(a)

    def start_move_to_shelf_layer(self):
        a = 'http://knowrob.org/kb/shop.owl#MoveToShelfLayer'
        if self.action_graph is not None:
            self.action_graph = self.action_graph.add_sub_action(a)

    def start_looking_at_location(self):
        a = 'http://knowrob.org/kb/knowrob.owl#LookingAtLocation'
        if self.action_graph is not None:
            self.action_graph = self.action_graph.add_sub_action(a)

    def start_base_movement(self, goal):
        a = 'http://knowrob.org/kb/motions.owl#BaseMovement'
        if self.action_graph is not None:
            self.action_graph = self.action_graph.add_sub_motion(a)

    def start_hed_movement(self, goal):
        a = 'http://knowrob.org/kb/knowrob_common.owl#HeadMovement'
        if self.action_graph is not None:
            self.action_graph = self.action_graph.add_sub_motion(a)


    # def start_scanning_action(self):
    #     action_type = 'http://knowrob.org/kb/knowrob.owl#LookingForSomething'
    #     self.action_graph = ActionGraph(self, action_type, msg='start scanning')

    # def start_movement(self, action_type='http://knowrob.org/kb/knowrob_common.owl#ArmMovement'):
    #     if self.action_graph is not None:
    #         self.action_graph = self.action_graph.add_sub_action(action_type, msg='muh')

    def finish_action(self):
        if self.action_graph is not None:
            self.action_graph = self.action_graph.finish()
