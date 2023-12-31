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

# Third Party
import torch

# CuRobo
from curobo.cuda_robot_model.cuda_robot_model import CudaRobotModel, CudaRobotModelConfig
from curobo.geom.sdf.world import CollisionCheckerType
from curobo.geom.types import WorldConfig
from curobo.types.base import TensorDeviceType
from curobo.types.math import Pose
from curobo.types.robot import JointState, RobotConfig
from curobo.util.usd_helper import UsdHelper
from curobo.util_file import (
    get_assets_path,
    get_robot_configs_path,
    get_world_configs_path,
    join_path,
    load_yaml,
)
from curobo.wrap.reacher.motion_gen import MotionGen, MotionGenConfig, MotionGenPlanConfig


def save_curobo_world_to_usd():
    world_file = "collision_table.yml"
    world_cfg = WorldConfig.from_dict(
        load_yaml(join_path(get_world_configs_path(), world_file))
    ).get_mesh_world(process=False)
    usd_helper = UsdHelper()
    usd_helper.create_stage()

    usd_helper.add_obstacles_to_stage(world_cfg)

    usd_helper.write_stage_to_file("test.usd")


def get_trajectory(robot_file="franka.yml", dt=1.0 / 60.0):
    tensor_args = TensorDeviceType()
    world_file = "collision_test.yml"
    motion_gen_config = MotionGenConfig.load_from_robot_config(
        robot_file,
        world_file,
        tensor_args,
        trajopt_tsteps=24,
        collision_checker_type=CollisionCheckerType.PRIMITIVE,
        use_cuda_graph=True,
        num_trajopt_seeds=2,
        num_graph_seeds=2,
        evaluate_interpolated_trajectory=True,
        interpolation_dt=dt,
        self_collision_check=True,
    )
    motion_gen = MotionGen(motion_gen_config)
    motion_gen.warmup()
    robot_cfg = load_yaml(join_path(get_robot_configs_path(), robot_file))["robot_cfg"]
    robot_cfg = RobotConfig.from_dict(robot_cfg, tensor_args)
    retract_cfg = motion_gen.get_retract_config()
    state = motion_gen.rollout_fn.compute_kinematics(
        JointState.from_position(retract_cfg.view(1, -1))
    )

    retract_pose = Pose(state.ee_pos_seq.squeeze(), quaternion=state.ee_quat_seq.squeeze())
    start_state = JointState.from_position(retract_cfg.view(1, -1).clone() + 0.5)
    # start_state.position[0,2] = 0.5
    # start_state.position[0,1] = 0.5
    # start_state.position[0,0] = 0.5
    # print(start_state.position)
    result = motion_gen.plan_single(start_state, retract_pose)
    # print(result.plan_state.position)
    print(result.success)
    # print(result.position_error)
    # result = motion_gen.plan(
    #    start_state, retract_pose, enable_graph=False, enable_opt=True, max_attempts=10
    # )
    traj = result.get_interpolated_plan()  # optimized plan
    return traj


def save_curobo_robot_world_to_usd(robot_file="franka.yml"):
    tensor_args = TensorDeviceType()
    world_file = "collision_test.yml"
    world_model = WorldConfig.from_dict(
        load_yaml(join_path(get_world_configs_path(), world_file))
    ).get_obb_world()
    dt = 1 / 60.0

    q_traj = get_trajectory(robot_file, dt)
    q_start = q_traj[0]

    UsdHelper.write_trajectory_animation_with_robot_usd(
        robot_file, world_model, q_start, q_traj, save_path="test.usd"
    )


def save_curobo_robot_to_usd(robot_file="franka.yml"):
    # print(robot_file)
    tensor_args = TensorDeviceType()
    robot_cfg_y = load_yaml(join_path(get_robot_configs_path(), robot_file))["robot_cfg"]
    robot_cfg_y["kinematics"]["use_usd_kinematics"] = True
    print(
        len(robot_cfg_y["kinematics"]["cspace"]["null_space_weight"]),
        len(robot_cfg_y["kinematics"]["cspace"]["retract_config"]),
        len(robot_cfg_y["kinematics"]["cspace"]["joint_names"]),
    )
    # print(robot_cfg_y)
    robot_cfg = RobotConfig.from_dict(robot_cfg_y, tensor_args)
    start = JointState.from_position(robot_cfg.cspace.retract_config)
    retract_cfg = robot_cfg.cspace.retract_config.clone()
    retract_cfg[0] = 0.5

    # print(retract_cfg)
    kin_model = CudaRobotModel(robot_cfg.kinematics)
    position = retract_cfg
    q_traj = JointState.from_position(position.unsqueeze(0))
    q_traj.joint_names = kin_model.joint_names
    # print(q_traj.joint_names)
    usd_helper = UsdHelper()
    # usd_helper.create_stage(
    #    "test.usd", timesteps=q_traj.position.shape[0] + 1, dt=dt, interpolation_steps=10
    # )
    world_file = "collision_table.yml"
    world_model = WorldConfig.from_dict(
        load_yaml(join_path(get_world_configs_path(), world_file))
    ).get_obb_world()

    # print(q_traj.position.shape)
    # usd_helper.load_robot_usd(robot_cfg.kinematics.usd_path, js)
    usd_helper.write_trajectory_animation_with_robot_usd(
        {"robot_cfg": robot_cfg_y},
        world_model,
        start,
        q_traj,
        save_path="test.usd",
        # robot_asset_prim_path="/robot"
    )

    # usd_helper.save()
    # usd_helper.write_stage_to_file("test.usda")


def read_world_from_usd(file_path: str):
    usd_helper = UsdHelper()
    usd_helper.load_stage_from_file(file_path)
    # world_model = usd_helper.get_obstacles_from_stage(reference_prim_path="/Root/world_obstacles")
    world_model = usd_helper.get_obstacles_from_stage(
        only_paths=["/world/obstacles"], reference_prim_path="/world"
    )
    # print(world_model)
    for x in world_model.cuboid:
        print(x.name + ":")
        print("  pose: ", x.pose)
        print("  dims: ", x.dims)


def read_robot_from_usd(robot_file: str = "franka.yml"):
    robot_cfg = load_yaml(join_path(get_robot_configs_path(), robot_file))["robot_cfg"]
    robot_cfg["kinematics"]["use_usd_kinematics"] = True
    robot_cfg = RobotConfig.from_dict(robot_cfg, TensorDeviceType())


if __name__ == "__main__":
    save_curobo_world_to_usd()
