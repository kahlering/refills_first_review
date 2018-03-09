#!/usr/bin/env python

from __future__ import print_function, division

import traceback
from simplejson import OrderedDict
import numpy as np
from time import time

import rospy
from geometry_msgs.msg import QuaternionStamped, Quaternion, PointStamped, Point, PoseStamped, Pose
from multiprocessing import TimeoutError
from std_msgs.msg import Header
from tf.transformations import quaternion_about_axis

from refills_first_review.baseboard_detection import BaseboardDetector
from refills_first_review.knowrob_wrapper import KnowRob
from refills_first_review.move_arm import GiskardWrapper
from refills_first_review.move_base import MoveBase
from refills_first_review.robosherlock_wrapper import RoboSherlock
from refills_first_review.separator_detection import SeparatorClustering

# base
FLOOR_SCANNING_OFFSET = {'x': 0.95,
                         'y': -0.15,
                         'z': -np.pi / 2}
FLOOR_DETECTION_OFFSET = {'x': 1.3,
                          'y': 0.5,
                          'z': -np.pi / 2}
# arm
# trans in camera_link, rot in base_footprint
COUNTING_OFFSET = {'trans': [0.0, -0.1, -0.1],
                   'rot': [0, 0.7071, -0.7071, 0]}

# in base_footprint
FLOOR_SCAN_POSE_BOTTOM = {'trans': [-.15, -.646, 0.177],
                          'rot': [0, 0.858, -0.514, 0]}
# in base_footprint
FLOOR_SCAN_POSE_REST = {'trans': [-.15, -.7, 0.0],
                        'rot': [0, 0.7071, -0.7071, 0]}
# in base_footprint
FLOOR_SCAN_POSE_HANGING = {'trans': [-.15, -.82, 0.0],
                        'rot': [0, 0.7071, -0.7071, 0]}
SHELF_BASEBOARD = PoseStamped(Header(0, rospy.Time(), 'base_footprint'),
                              Pose(Point(-0.137, -0.68, 0.223),
                                   Quaternion(-0.000, 0.841, -0.541, 0.000)))


class CRAM(object):
    def __init__(self):
        # TODO use paramserver [low]
        # TODO publish own shelf frames? [low]
        # TODO live logging [high]
        # TODO SMS [high]
        self.counting_enabled = True
        self.knowrob = KnowRob()
        self.robosherlock = RoboSherlock(self.counting_enabled)
        self.move_base = MoveBase(enabled=True)
        self.move_arm = GiskardWrapper(enabled=True)
        self.map_frame_id = rospy.get_param('~/map', 'map')

    def scan_shop(self):
        # TODO make sure that nothing is close [medium]
        self.move_arm.drive_pose()
        self.detect_baseboards()
        self.move_arm.drive_pose()
        for shelf_id in self.knowrob.get_shelves():
            rospy.loginfo('scanning shelf \'{}\''.format(shelf_id))
            t = time()
            self.scan_shelf(shelf_id)
            rospy.loginfo('scanned shelf \'{}\' in {:.2f}s'.format(shelf_id, time() - t))

    def detect_baseboards(self):
        rospy.loginfo('shelf baseboard detection requires manuel mode')
        rospy.loginfo('move to free space plx')
        cmd = raw_input('done? [y]')
        if cmd.isdigit():
            rospy.logwarn('skipping baseboard detection')
            self.robosherlock.baseboard_detection.detect_fake_shelves(cmd)
            self.robosherlock.start_baseboard_detection()
        else:
            if cmd == 'y':
                rospy.loginfo('moving arm to baseboard scanning pose')
                self.move_arm.pre_baseboard_pose()
                self.move_arm.set_and_send_cartesian_goal(SHELF_BASEBOARD)
            else:
                raise UserWarning('you dumb... ABORT!!')
            rospy.loginfo('scan all shelf baseboard plx')

            self.robosherlock.start_baseboard_detection()

            cmd = raw_input('done? [y]')
            if cmd != 'y':
                raise UserWarning('you dumb... ABORT!!')

            # TODO check if shit is really save [medium]
            rospy.loginfo('MAKE SURE NOTHING IS CLOSE!!!!11elf')
            cmd = raw_input('rdy? [y]')
            if cmd != 'y' or self.move_base.is_stuff_close():
                raise UserWarning('you dumb... ABORT!!')

        shelves = self.robosherlock.stop_baseboard_detection()
        self.knowrob.add_shelves(shelves)

    def scan_shelf(self, shelf_id):
        self.detect_shelf_floors(shelf_id)
        for shelf_floor_id in self.knowrob.get_floor_ids(shelf_id):
            if not self.knowrob.is_floor_too_high(shelf_id, shelf_floor_id):
                self.scan_floor(shelf_id, shelf_floor_id)
                if self.counting_enabled and not self.knowrob.is_hanging_foor(shelf_id, shelf_floor_id):
                    self.count_floor(shelf_id, shelf_floor_id)
        self.move_arm.drive_pose()

    def detect_shelf_floors(self, shelf_id):
        self.go_into_floor_detection_pose(shelf_id)
        floor_heights = self.robosherlock.detect_floors(shelf_id)
        self.knowrob.add_shelf_floors(shelf_id, floor_heights)

    def go_into_floor_detection_pose(self, shelf_id):
        self.move_base.move_absolute_xyz(self.knowrob.get_shelf_frame_id(shelf_id),
                                         FLOOR_DETECTION_OFFSET['x'],
                                         FLOOR_DETECTION_OFFSET['y'],
                                         FLOOR_DETECTION_OFFSET['z'])
        self.move_arm.floor_detection_pose()

    def scan_floor(self, shelf_id, floor_id):
        # TODO don't look straight onto the barcodes
        rospy.loginfo('scanning floor {}/{}'.format(shelf_id, floor_id))
        self.set_floor_scan_pose(shelf_id, floor_id)
        self.move_arm.send_cartesian_goal()
        if floor_id % 2 == 0 or self.counting_enabled:
            self.move_in_front_of_shelf(shelf_id)

        if not self.knowrob.is_hanging_foor(shelf_id, floor_id):
            self.robosherlock.start_separator_detection(shelf_id, floor_id)
        self.robosherlock.start_barcode_detection(shelf_id, floor_id)

        try:
            if floor_id % 2 == 0 or self.counting_enabled:
                #TODO hack while counting not implemented
                self.move_base.move_relative([-self.knowrob.get_floor_width(), 0, 0])
            else:
                self.move_in_front_of_shelf(shelf_id)
        except TimeoutError as e:
            self.move_base.STOP()


        if not self.knowrob.is_hanging_foor(shelf_id, floor_id):
            separators = self.robosherlock.stop_separator_detection()
            self.knowrob.add_separators(shelf_id, floor_id, separators)
        barcodes = self.robosherlock.stop_barcode_detection()
        self.knowrob.add_barcodes(barcodes)

    def set_floor_scan_pose(self, shelf_id, floor_id):
        if self.knowrob.is_bottom_floor(shelf_id, floor_id):
            pose = FLOOR_SCAN_POSE_BOTTOM
        elif self.knowrob.is_hanging_foor(shelf_id, floor_id):
            pose = FLOOR_SCAN_POSE_HANGING
        else:
            pose = FLOOR_SCAN_POSE_REST
        self.move_arm.set_orientation_goal(QuaternionStamped(Header(0, rospy.Time(), self.move_arm.root),
                                                             Quaternion(*pose['rot'])))
        self.move_arm.set_translation_goal(
            PointStamped(Header(0, rospy.Time(), self.move_arm.root),
                         Point(pose['trans'][0],
                               pose['trans'][1],
                               pose['trans'][2] + self.knowrob.get_floor_height(shelf_id, floor_id))))

    def move_in_front_of_shelf(self, shelf_id):
        return self.move_base.move_absolute_xyz(self.knowrob.get_shelf_frame_id(shelf_id),
                                                FLOOR_SCANNING_OFFSET['x'],
                                                FLOOR_SCANNING_OFFSET['y'],
                                                FLOOR_SCANNING_OFFSET['z'])

    def count_floor(self, shelf_id, floor_id):
        rospy.loginfo('counting objects on floor {}/{}'.format(shelf_id, floor_id))

        facings = self.knowrob.get_facings(shelf_id, floor_id)
        self.move_arm.set_orientation_goal(QuaternionStamped(Header(0, rospy.Time(), self.move_arm.root),
                                                             Quaternion(*COUNTING_OFFSET['rot'])))
        self.move_arm.set_translation_goal(PointStamped(Header(0, rospy.Time(), self.move_arm.tip),
                                                        Point(*COUNTING_OFFSET['trans'])))
        self.move_arm.send_cartesian_goal()
        if len(facings) == 0:
            self.move_base.move_relative([self.knowrob.get_floor_width(), 0, 0])
        else:
            # muh = 0
            for i, facing_y in enumerate(reversed(sorted(facings))):
                # self.move_base.move_relative([1+facing_x-muh, 0, 0])
                # muh = 1+facing_x
                self.move_base.move_absolute_xyz(self.knowrob.get_shelf_frame_id(shelf_id),
                                                 FLOOR_SCANNING_OFFSET['x'],
                                                 FLOOR_SCANNING_OFFSET['y'] + facing_y,
                                                 FLOOR_SCANNING_OFFSET['z'])
                count = self.robosherlock.count()
                # TODO get name of object in facing [medium]
                rospy.loginfo('counted {} {} times'.format('muh', count))

    def STOP(self):
        self.move_base.STOP()
        self.move_arm.client.cancel_goal()
        self.move_arm.client.cancel_all_goals()


if __name__ == '__main__':
    rospy.init_node('brain')
    cram = CRAM()
    # cram.STOP()
    try:
        cmd = raw_input('start demo? [y]')
        if cmd == 'y':
            rospy.loginfo('starting REFILLS scenario 1 demo')
            cram.scan_shop()
            rospy.loginfo('REFILLS scenario 1 demo completed')
    except Exception as e:
        traceback.print_exc()
    finally:
        rospy.loginfo('canceling all goals')
        cram.STOP()
        rospy.sleep(1)
