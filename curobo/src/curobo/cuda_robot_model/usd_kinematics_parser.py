#
# Copyright (c) 2023 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# Standard Library
from typing import Dict, List, Optional, Tuple

# Third Party
import numpy as np
from pxr import Usd, UsdPhysics

# CuRobo
from curobo.cuda_robot_model.kinematics_parser import KinematicsParser, LinkParams
from curobo.cuda_robot_model.types import JointType
from curobo.types.base import TensorDeviceType
from curobo.types.math import Pose
from curobo.util.logger import log_error


class UsdKinematicsParser(KinematicsParser):
    """An experimental kinematics parser from USD.
    NOTE: A more complete solution will be available in a future release. Current implementation
    does not account for link geometry transformations after a joints.
    """

    def __init__(
        self,
        usd_path: str,
        flip_joints: List[str] = [],
        flip_joint_limits: List[str] = [],
        usd_robot_root: str = "robot",
        tensor_args: TensorDeviceType = TensorDeviceType(),
        extra_links: Optional[Dict[str, LinkParams]] = None,
    ) -> None:
        # load usd file:

        # create a usd stage
        self._flip_joints = flip_joints
        self._flip_joint_limits = flip_joint_limits
        self._stage = Usd.Stage.Open(usd_path)
        self._usd_robot_root = usd_robot_root
        self._parent_joint_map = {}
        self.tensor_args = tensor_args
        super().__init__(extra_links)

    @property
    def robot_prim_root(self):
        return self._usd_robot_root

    def build_link_parent(self):
        self._parent_map = {}
        all_joints = [
            x
            for x in self._stage.Traverse()
            if (x.IsA(UsdPhysics.Joint) and str(x.GetPath()).startswith(self._usd_robot_root))
        ]
        for l in all_joints:
            parent, child = get_links_for_joint(l)
            if child is not None and parent is not None:
                self._parent_map[child.GetName()] = parent.GetName()
                self._parent_joint_map[child.GetName()] = l  # store joint prim

    def get_link_parameters(self, link_name: str, base: bool = False) -> LinkParams:
        """Get Link parameters from usd stage.

        NOTE: USD kinematics "X" axis joints map to "Z" in URDF. Specifically,
        uniform token physics:axis = "X" value only matches "Z" in URDF. This is because of usd
        files assuming Y axis as up while urdf files assume Z axis as up.

        Args:
            link_name (str): Name of link.
            base (bool, optional): flag to specify base link. Defaults to False.

        Returns:
            LinkParams: obtained link parameters.
        """
        link_params = self._get_from_extra_links(link_name)
        if link_params is not None:
            return link_params
        joint_limits = None
        joint_axis = None
        if base:
            parent_link_name = None
            joint_transform = np.eye(4)
            joint_name = "base_joint"
            joint_type = JointType.FIXED

        else:
            parent_link_name = self._parent_map[link_name]
            joint_prim = self._parent_joint_map[link_name]  # joint prim connects link
            joint_transform = self._get_joint_transform(joint_prim)
            joint_axis = None
            joint_name = joint_prim.GetName()
            if joint_prim.IsA(UsdPhysics.FixedJoint):
                joint_type = JointType.FIXED
            elif joint_prim.IsA(UsdPhysics.RevoluteJoint):
                j_prim = UsdPhysics.RevoluteJoint(joint_prim)
                joint_axis = j_prim.GetAxisAttr().Get()
                joint_limits = np.radians(
                    np.ravel([j_prim.GetLowerLimitAttr().Get(), j_prim.GetUpperLimitAttr().Get()])
                )
                if joint_name in self._flip_joints.keys():
                    joint_axis = self._flip_joints[joint_name]
                if joint_axis == "X":
                    joint_type = JointType.X_ROT
                elif joint_axis == "Y":
                    joint_type = JointType.Y_ROT
                elif joint_axis == "Z":
                    joint_type = JointType.Z_ROT
                else:
                    log_error("Joint axis not supported" + str(joint_axis))

            elif joint_prim.IsA(UsdPhysics.PrismaticJoint):
                j_prim = UsdPhysics.PrismaticJoint(joint_prim)

                joint_axis = j_prim.GetAxisAttr().Get()
                joint_limits = np.ravel(
                    [j_prim.GetLowerLimitAttr().Get(), j_prim.GetUpperLimitAttr().Get()]
                )
                if joint_name in self._flip_joints.keys():
                    joint_axis = self._flip_joints[joint_name]
                if joint_name in self._flip_joint_limits:
                    joint_limits = np.ravel(
                        [-1.0 * j_prim.GetUpperLimitAttr().Get(), j_prim.GetLowerLimitAttr().Get()]
                    )
                if joint_axis == "X":
                    joint_type = JointType.X_PRISM
                elif joint_axis == "Y":
                    joint_type = JointType.Y_PRISM
                elif joint_axis == "Z":
                    joint_type = JointType.Z_PRISM
                else:
                    log_error("Joint axis not supported" + str(joint_axis))
            else:
                log_error("Joint type not supported")
        link_params = LinkParams(
            link_name=link_name,
            joint_name=joint_name,
            joint_type=joint_type,
            fixed_transform=joint_transform,
            parent_link_name=parent_link_name,
            joint_limits=joint_limits,
        )
        return link_params

    def _get_joint_transform(self, prim: Usd.Prim):
        j_prim = UsdPhysics.Joint(prim)
        position = np.ravel(j_prim.GetLocalPos0Attr().Get())
        quatf = j_prim.GetLocalRot0Attr().Get()
        quat = np.zeros(4)
        quat[0] = quatf.GetReal()
        quat[1:] = quatf.GetImaginary()

        # create a homogenous transformation matrix:
        transform_0 = Pose(self.tensor_args.to_device(position), self.tensor_args.to_device(quat))

        position = np.ravel(j_prim.GetLocalPos1Attr().Get())
        quatf = j_prim.GetLocalRot1Attr().Get()
        quat = np.zeros(4)
        quat[0] = quatf.GetReal()
        quat[1:] = quatf.GetImaginary()

        # create a homogenous transformation matrix:
        transform_1 = Pose(self.tensor_args.to_device(position), self.tensor_args.to_device(quat))
        transform = (
            transform_0.multiply(transform_1.inverse()).get_matrix().cpu().view(4, 4).numpy()
        )

        # get attached link transform:

        return transform


def get_links_for_joint(prim: Usd.Prim) -> Tuple[Optional[Usd.Prim], Optional[Usd.Prim]]:
    """Get all link prims from the given joint prim.

    Note:
        This assumes that the `body0_rel_targets` and `body1_rel_targets` are configured such
        that the parent link is specified in `body0_rel_targets` and the child links is specified
        in `body1_rel_targets`.
    """
    stage = prim.GetStage()
    joint_api = UsdPhysics.Joint(prim)

    rel0_targets = joint_api.GetBody0Rel().GetTargets()
    if len(rel0_targets) > 1:
        raise NotImplementedError(
            "`get_links_for_joint` does not currently handle more than one relative"
            f" body target in the joint. joint_prim: {prim}, body0_rel_targets:"
            f" {rel0_targets}"
        )
    link0_prim = None
    if len(rel0_targets) != 0:
        link0_prim = stage.GetPrimAtPath(rel0_targets[0])

    rel1_targets = joint_api.GetBody1Rel().GetTargets()
    if len(rel1_targets) > 1:
        raise NotImplementedError(
            "`get_links_for_joint` does not currently handle more than one relative"
            f" body target in the joint. joint_prim: {prim}, body1_rel_targets:"
            f" {rel0_targets}"
        )
    link1_prim = None
    if len(rel1_targets) != 0:
        link1_prim = stage.GetPrimAtPath(rel1_targets[0])

    return (link0_prim, link1_prim)
