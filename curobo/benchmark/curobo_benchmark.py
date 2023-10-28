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
from copy import deepcopy
from typing import Optional

# Third Party
import numpy as np
import torch
from tqdm import tqdm

# CuRobo
from curobo.geom.sdf.world import CollisionCheckerType, WorldConfig
from curobo.geom.types import Mesh
from curobo.types.base import TensorDeviceType
from curobo.types.math import Pose
from curobo.types.robot import RobotConfig
from curobo.types.state import JointState
from curobo.util.logger import setup_curobo_logger
from curobo.util_file import (
    get_assets_path,
    get_robot_configs_path,
    get_world_configs_path,
    join_path,
    load_yaml,
    write_yaml,
)
from curobo.wrap.reacher.motion_gen import MotionGen, MotionGenConfig, MotionGenPlanConfig

# torch.set_num_threads(8)
# torch.use_deterministic_algorithms(True)
torch.manual_seed(0)

torch.backends.cudnn.benchmark = True

torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True

# torch.backends.cuda.matmul.allow_tf32 = False
# torch.backends.cudnn.allow_tf32 = False

# torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = True
np.random.seed(0)
# Standard Library
import argparse
import warnings
from typing import List, Optional

# Third Party
from metrics import CuroboGroupMetrics, CuroboMetrics
from robometrics.datasets import demo_raw, motion_benchmaker_raw, mpinets_raw


def plot_cost_iteration(cost: torch.Tensor, save_path="cost", title="", log_scale=False):
    # Third Party
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(5, 4))
    cost = cost.cpu().numpy()
    # save to csv:
    np.savetxt(save_path + ".csv", cost, delimiter=",")

    # if cost.shape[0] > 1:
    colormap = plt.cm.winter
    plt.gca().set_prop_cycle(plt.cycler("color", colormap(np.linspace(0, 1, cost.shape[0]))))
    x = [i for i in range(cost.shape[-1])]
    for i in range(cost.shape[0]):
        plt.plot(x, cost[i], label="seed_" + str(i))
    plt.tight_layout()
    # plt.title(title)
    plt.xlabel("iteration")
    plt.ylabel("cost")
    if log_scale:
        plt.yscale("log")
    plt.grid()
    # plt.legend()
    plt.tight_layout()
    plt.savefig(save_path + ".pdf")
    plt.close()


def plot_traj(act_seq: JointState, dt=0.25, title="", save_path="plot.png", sma_filter=False):
    # Third Party
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(4, 1, figsize=(5, 8), sharex=True)
    t_steps = np.linspace(0, act_seq.position.shape[0] * dt, act_seq.position.shape[0])
    # compute acceleration from finite difference of velocity:
    # act_seq.acceleration = (torch.roll(act_seq.velocity, -1, 0) - act_seq.velocity) / dt
    # act_seq.acceleration = ( act_seq.velocity - torch.roll(act_seq.velocity, 1, 0)) / dt
    # act_seq.acceleration[0,:] = 0.0
    # act_seq.jerk = ( act_seq.acceleration - torch.roll(act_seq.acceleration, 1, 0)) / dt
    # act_seq.jerk[0,:] = 0.0
    if sma_filter:
        kernel = 5
        sma = torch.nn.AvgPool1d(kernel_size=kernel, stride=1, padding=2, ceil_mode=False).cuda()
    # act_seq.jerk = sma(act_seq.jerk)
    # act_seq.acceleration[-1,:] = 0.0
    for i in range(act_seq.position.shape[-1]):
        ax[0].plot(t_steps, act_seq.position[:, i].cpu(), "-", label=str(i))
        # act_seq.velocity[1:-1, i] = sma(act_seq.velocity[:,i].view(1,-1)).squeeze()#@[1:-2]

        ax[1].plot(t_steps[: act_seq.velocity.shape[0]], act_seq.velocity[:, i].cpu(), "-")
        if sma_filter:
            act_seq.acceleration[:, i] = sma(
                act_seq.acceleration[:, i].view(1, -1)
            ).squeeze()  # @[1:-2]

        ax[2].plot(t_steps[: act_seq.acceleration.shape[0]], act_seq.acceleration[:, i].cpu(), "-")
        if sma_filter:
            act_seq.jerk[:, i] = sma(act_seq.jerk[:, i].view(1, -1)).squeeze()  # @[1:-2]\

        ax[3].plot(t_steps[: act_seq.jerk.shape[0]], act_seq.jerk[:, i].cpu(), "-")
    ax[0].set_title(title + " dt=" + "{:.3f}".format(dt))
    ax[3].set_xlabel("Time(s)")
    ax[3].set_ylabel("Jerk rad. s$^{-3}$")
    ax[0].set_ylabel("Position rad.")
    ax[1].set_ylabel("Velocity rad. s$^{-1}$")
    ax[2].set_ylabel("Acceleration rad. s$^{-2}$")
    ax[0].grid()
    ax[1].grid()
    ax[2].grid()
    ax[3].grid()
    # ax[0].legend(loc="upper right")
    ax[0].legend(bbox_to_anchor=(0.5, 1.6), loc="upper center", ncol=4)
    plt.tight_layout()
    plt.savefig(save_path)
    plt.close()
    # plt.legend()


def load_curobo(
    n_cubes: int,
    enable_debug: bool = False,
    tsteps: int = 30,
    trajopt_seeds: int = 4,
    mpinets: bool = False,
    graph_mode: bool = True,
    mesh_mode: bool = False,
    cuda_graph: bool = True,
    collision_buffer: float = -0.01,
):
    robot_cfg = load_yaml(join_path(get_robot_configs_path(), "franka.yml"))["robot_cfg"]
    robot_cfg["kinematics"]["collision_sphere_buffer"] = collision_buffer
    robot_cfg["kinematics"]["collision_spheres"] = "spheres/franka_mesh.yml"
    robot_cfg["kinematics"]["collision_link_names"].remove("attached_object")

    # del robot_cfg["kinematics"]

    ik_seeds = 30  # 500
    if graph_mode:
        trajopt_seeds = 4
    if trajopt_seeds >= 14:
        ik_seeds = max(100, trajopt_seeds * 4)
    if mpinets:
        robot_cfg["kinematics"]["lock_joints"] = {
            "panda_finger_joint1": 0.025,
            "panda_finger_joint2": -0.025,
        }
    world_cfg = WorldConfig.from_dict(
        load_yaml(join_path(get_world_configs_path(), "collision_table.yml"))
    ).get_obb_world()
    interpolation_steps = 2000
    c_checker = CollisionCheckerType.PRIMITIVE
    c_cache = {"obb": n_cubes}
    if mesh_mode:
        c_checker = CollisionCheckerType.MESH
        c_cache = {"mesh": n_cubes}
        world_cfg = world_cfg.get_mesh_world()
    if graph_mode:
        interpolation_steps = 100

    robot_cfg_instance = RobotConfig.from_dict(robot_cfg, tensor_args=TensorDeviceType())

    K = robot_cfg_instance.kinematics.kinematics_config.joint_limits
    K.position[0, :] -= 0.1
    K.position[1, :] += 0.1

    motion_gen_config = MotionGenConfig.load_from_robot_config(
        robot_cfg_instance,
        world_cfg,
        trajopt_tsteps=tsteps,
        collision_checker_type=c_checker,
        use_cuda_graph=cuda_graph,
        collision_cache=c_cache,
        position_threshold=0.005,  # 5 mm
        rotation_threshold=0.05,
        num_ik_seeds=ik_seeds,
        num_graph_seeds=trajopt_seeds,
        num_trajopt_seeds=trajopt_seeds,
        interpolation_dt=0.025,
        store_ik_debug=enable_debug,
        store_trajopt_debug=enable_debug,
        interpolation_steps=interpolation_steps,
        collision_activation_distance=0.03,
        trajopt_dt=0.25,
        finetune_dt_scale=1.05,  # 1.05,
        maximum_trajectory_dt=0.1,
    )
    mg = MotionGen(motion_gen_config)
    mg.warmup(enable_graph=True, warmup_js_trajopt=False)
    return mg, robot_cfg


def benchmark_mb(
    write_usd=False,
    save_log=False,
    write_plot=False,
    write_benchmark=False,
    plot_cost=False,
    override_tsteps: Optional[int] = None,
    save_kpi=False,
    graph_mode=False,
    args=None,
):
    # load dataset:

    interpolation_dt = 0.02
    # mpinets_data = True
    # if mpinets_data:
    file_paths = [motion_benchmaker_raw, mpinets_raw][:]
    if args.demo:
        file_paths = [demo_raw]

    # else:22
    #    file_paths = [get_mb_dataset_path()][:1]
    enable_debug = save_log or plot_cost
    all_files = []
    og_tsteps = 32
    if override_tsteps is not None:
        og_tsteps = override_tsteps

    og_trajopt_seeds = 12
    for file_path in file_paths:
        all_groups = []
        mpinets_data = False
        problems = file_path()
        if "dresser_task_oriented" in list(problems.keys()):
            mpinets_data = True
        for key, v in tqdm(problems.items()):
            tsteps = og_tsteps
            trajopt_seeds = og_trajopt_seeds

            if "cage_panda" in key:
                trajopt_seeds = 16
            # else:
            #    continue
            if "table_under_pick_panda" in key:
                tsteps = 44
                trajopt_seeds = 28

            if "cubby_task_oriented" in key and "merged" not in key:
                trajopt_seeds = 16
            if "dresser_task_oriented" in key:
                trajopt_seeds = 16
            scene_problems = problems[key]  # [:4]  # [:1]  # [:20]  # [0:10]
            n_cubes = check_problems(scene_problems)
            # torch.cuda.empty_cache()
            mg, robot_cfg = load_curobo(
                n_cubes,
                enable_debug,
                tsteps,
                trajopt_seeds,
                mpinets_data,
                graph_mode,
                args.mesh,
                not args.disable_cuda_graph,
                collision_buffer=args.collision_buffer,
            )
            m_list = []
            i = 0
            ik_fail = 0
            for problem in tqdm(scene_problems, leave=False):
                i += 1
                if problem["collision_buffer_ik"] < 0.0:
                    #    print("collision_ik:", problem["collision_buffer_ik"])
                    continue
                # if i != 269: # 226
                #    continue

                plan_config = MotionGenPlanConfig(
                    max_attempts=100,  # 00,  # 00,  # 100,  # 00,  # 000,#,00,#00,  # 5000,
                    enable_graph_attempt=3,
                    enable_finetune_trajopt=True,
                    partial_ik_opt=False,
                    enable_graph=graph_mode,
                    timeout=60,
                    enable_opt=not graph_mode,
                )
                # if "table_under_pick_panda" in key:
                #    plan_config.enable_graph = True
                #    plan_config.partial_ik_opt = False
                q_start = problem["start"]
                pose = (
                    problem["goal_pose"]["position_xyz"] + problem["goal_pose"]["quaternion_wxyz"]
                )
                problem_name = "d_" + key + "_" + str(i)

                # reset planner
                mg.reset(reset_seed=False)
                if args.mesh:
                    world = WorldConfig.from_dict(deepcopy(problem["obstacles"])).get_mesh_world()
                else:
                    world = WorldConfig.from_dict(deepcopy(problem["obstacles"])).get_obb_world()
                mg.world_coll_checker.clear_cache()
                mg.update_world(world)
                # continue
                # load obstacles

                # run planner
                start_state = JointState.from_position(mg.tensor_args.to_device([q_start]))
                goal_pose = Pose.from_list(pose)

                result = mg.plan_single(
                    start_state,
                    goal_pose,
                    plan_config,
                )
                if result.status == "IK Fail":
                    ik_fail += 1
                # rint(plan_config.enable_graph, plan_config.enable_graph_attempt)
                problem["solution"] = None
                if plan_config.enable_finetune_trajopt:
                    problem_name = key + "_" + str(i)
                else:
                    problem_name = "noft_" + key + "_" + str(i)
                problem_name = "nosw_" + problem_name
                if write_usd or save_log:
                    # CuRobo
                    from curobo.util.usd_helper import UsdHelper

                    world.randomize_color(r=[0.5, 0.9], g=[0.2, 0.5], b=[0.0, 0.2])

                    gripper_mesh = Mesh(
                        name="target_gripper",
                        file_path=join_path(
                            get_assets_path(),
                            "robot/franka_description/meshes/visual/hand.dae",
                        ),
                        color=[0.0, 0.8, 0.1, 1.0],
                        pose=pose,
                    )
                    world.add_obstacle(gripper_mesh)
                # get costs:
                if plot_cost:
                    dt = 0.5
                    problem_name = "approx_wolfe_p" + problem_name
                    if result.optimized_dt is not None:
                        dt = result.optimized_dt.item()
                    if "trajopt_result" in result.debug_info:
                        success = result.success.item()
                        traj_cost = (
                            # result.debug_info["trajopt_result"].debug_info["solver"]["cost"][0]
                            result.debug_info["trajopt_result"].debug_info["solver"]["cost"][-1]
                        )
                        # print(traj_cost[0])
                        traj_cost = torch.cat(traj_cost, dim=-1)
                        plot_cost_iteration(
                            traj_cost,
                            title=problem_name + "_" + str(success) + "_" + str(dt),
                            save_path=join_path("log/plot/", problem_name + "_cost"),
                            log_scale=False,
                        )
                        if "finetune_trajopt_result" in result.debug_info:
                            traj_cost = result.debug_info["finetune_trajopt_result"].debug_info[
                                "solver"
                            ]["cost"][0]
                            traj_cost = torch.cat(traj_cost, dim=-1)
                            plot_cost_iteration(
                                traj_cost,
                                title=problem_name + "_" + str(success) + "_" + str(dt),
                                save_path=join_path("log/plot/", problem_name + "_ft_cost"),
                                log_scale=False,
                            )
                if result.success.item():
                    # print("GT: ", result.graph_time)
                    q_traj = result.get_interpolated_plan()
                    problem["goal_ik"] = q_traj.position.cpu().squeeze().numpy()[-1, :].tolist()
                    problem["solution"] = {
                        "position": result.get_interpolated_plan()
                        .position.cpu()
                        .squeeze()
                        .numpy()
                        .tolist(),
                        "velocity": result.get_interpolated_plan()
                        .velocity.cpu()
                        .squeeze()
                        .numpy()
                        .tolist(),
                        "acceleration": result.get_interpolated_plan()
                        .acceleration.cpu()
                        .squeeze()
                        .numpy()
                        .tolist(),
                        "jerk": result.get_interpolated_plan()
                        .jerk.cpu()
                        .squeeze()
                        .numpy()
                        .tolist(),
                        "dt": interpolation_dt,
                    }
                    # print(problem["solution"]["position"])
                    # exit()
                    debug = {
                        "used_graph": result.used_graph,
                        "attempts": result.attempts,
                        "ik_time": result.ik_time,
                        "graph_time": result.graph_time,
                        "trajopt_time": result.trajopt_time,
                        "total_time": result.total_time,
                        "solve_time": result.solve_time,
                        "opt_traj": {
                            "position": result.optimized_plan.position.cpu()
                            .squeeze()
                            .numpy()
                            .tolist(),
                            "velocity": result.optimized_plan.velocity.cpu()
                            .squeeze()
                            .numpy()
                            .tolist(),
                            "acceleration": result.optimized_plan.acceleration.cpu()
                            .squeeze()
                            .numpy()
                            .tolist(),
                            "jerk": result.optimized_plan.jerk.cpu().squeeze().numpy().tolist(),
                            "dt": result.optimized_dt.item(),
                        },
                        "valid_query": result.valid_query,
                    }
                    problem["solution_debug"] = debug
                    # print(
                    #    "T: ",
                    #    result.motion_time.item(),
                    #    result.optimized_dt.item(),
                    #    (len(problem["solution"]["position"]) - 1 ) * result.interpolation_dt,
                    #    result.interpolation_dt,
                    #    )
                    # exit()
                    reached_pose = mg.compute_kinematics(result.optimized_plan[-1]).ee_pose
                    rot_error = goal_pose.angular_distance(reached_pose) * 100.0
                    if args.graph:
                        solve_time = result.graph_time
                    else:
                        solve_time = result.solve_time
                    current_metrics = CuroboMetrics(
                        skip=False,
                        success=True,
                        time=result.total_time,
                        collision=False,
                        joint_limit_violation=False,
                        self_collision=False,
                        position_error=result.position_error.item() * 1000.0,
                        orientation_error=rot_error.item(),
                        eef_position_path_length=10,
                        eef_orientation_path_length=10,
                        attempts=result.attempts,
                        motion_time=result.motion_time.item(),
                        solve_time=solve_time,
                    )

                    if write_usd:
                        # CuRobo

                        q_traj = result.get_interpolated_plan()
                        UsdHelper.write_trajectory_animation_with_robot_usd(
                            robot_cfg,
                            world,
                            start_state,
                            q_traj,
                            dt=result.interpolation_dt,
                            save_path=join_path("log/usd/", problem_name) + ".usd",
                            interpolation_steps=1,
                            write_robot_usd_path="log/usd/assets/",
                            robot_usd_local_reference="assets/",
                            base_frame="/world_" + problem_name,
                            visualize_robot_spheres=True,
                        )

                    if write_plot:
                        problem_name = problem_name
                        plot_traj(
                            result.optimized_plan,
                            result.optimized_dt.item(),
                            # result.get_interpolated_plan(),
                            # result.interpolation_dt,
                            title=problem_name,
                            save_path=join_path("log/plot/", problem_name + ".pdf"),
                        )
                        plot_traj(
                            # result.optimized_plan,
                            # result.optimized_dt.item(),
                            result.get_interpolated_plan(),
                            result.interpolation_dt,
                            title=problem_name,
                            save_path=join_path("log/plot/", problem_name + "_int.pdf"),
                        )
                        # exit()

                    m_list.append(current_metrics)
                    all_groups.append(current_metrics)
                elif result.valid_query:
                    # print("fail")
                    # print(result.status)
                    current_metrics = CuroboMetrics()
                    debug = {
                        "used_graph": result.used_graph,
                        "attempts": result.attempts,
                        "ik_time": result.ik_time,
                        "graph_time": result.graph_time,
                        "trajopt_time": result.trajopt_time,
                        "total_time": result.total_time,
                        "solve_time": result.solve_time,
                        "status": result.status,
                        "valid_query": result.valid_query,
                    }
                    problem["solution_debug"] = debug

                    m_list.append(current_metrics)
                    all_groups.append(current_metrics)
                else:
                    # print("invalid: " + problem_name)
                    debug = {
                        "used_graph": result.used_graph,
                        "attempts": result.attempts,
                        "ik_time": result.ik_time,
                        "graph_time": result.graph_time,
                        "trajopt_time": result.trajopt_time,
                        "total_time": result.total_time,
                        "solve_time": result.solve_time,
                        "status": result.status,
                        "valid_query": result.valid_query,
                    }

                    problem["solution_debug"] = debug
                    if False:
                        world.save_world_as_mesh(problem_name + ".obj")

                        q_traj = start_state  # .unsqueeze(0)
                        # CuRobo
                        from curobo.util.usd_helper import UsdHelper

                        UsdHelper.write_trajectory_animation_with_robot_usd(
                            robot_cfg,
                            world,
                            start_state,
                            q_traj,
                            dt=result.interpolation_dt,
                            save_path=join_path("log/usd/", problem_name) + ".usd",
                            interpolation_steps=1,
                            write_robot_usd_path="log/usd/assets/",
                            robot_usd_local_reference="assets/",
                            base_frame="/world_" + problem_name,
                            visualize_robot_spheres=True,
                        )
                if save_log:  # and not result.success.item():
                    # print("save log")
                    UsdHelper.write_motion_gen_log(
                        result,
                        robot_cfg,
                        world,
                        start_state,
                        Pose.from_list(pose),
                        join_path("log/usd/", problem_name) + "_debug",
                        write_ik=False,
                        write_trajopt=True,
                        visualize_robot_spheres=False,
                        grid_space=2,
                    )
                # exit()

            g_m = CuroboGroupMetrics.from_list(m_list)
            if not args.kpi:
                print(
                    key,
                    f"{g_m.success:2.2f}",
                    # g_m.motion_time,
                    g_m.time.mean,
                    # g_m.time.percent_75,
                    g_m.time.percent_98,
                    g_m.position_error.percent_98,
                    # g_m.position_error.median,
                    g_m.orientation_error.percent_98,
                    # g_m.orientation_error.median,
                )  # , g_m.attempts)
                print(g_m.attempts)
            # print("MT: ", g_m.motion_time)
            # $print(ik_fail)
            # exit()
            # print(g_m.position_error, g_m.orientation_error)

        g_m = CuroboGroupMetrics.from_list(all_groups)
        if not args.kpi:
            print(
                "All: ",
                f"{g_m.success:2.2f}",
                g_m.motion_time.percent_98,
                g_m.time.mean,
                g_m.time.percent_75,
                g_m.position_error.percent_75,
                g_m.orientation_error.percent_75,
            )  # g_m.time, g_m.attempts)
        # print("MT: ", g_m.motion_time)

        # print(g_m.position_error, g_m.orientation_error)

        # exit()
        if write_benchmark:
            if not mpinets_data:
                write_yaml(problems, args.file_name + "_mb_solution.yaml")
            else:
                write_yaml(problems, args.file_name + "_mpinets_solution.yaml")
        all_files += all_groups
    g_m = CuroboGroupMetrics.from_list(all_files)
    # print(g_m.success, g_m.time, g_m.attempts, g_m.position_error, g_m.orientation_error)
    print("######## FULL SET ############")
    print("All: ", f"{g_m.success:2.2f}")
    print("MT: ", g_m.motion_time)
    print("PT:", g_m.time)
    print("ST: ", g_m.solve_time)
    print("position error (mm): ", g_m.position_error)
    print("orientation error(%): ", g_m.orientation_error)

    if args.kpi:
        kpi_data = {
            "Success": g_m.success,
            "Planning Time Mean": float(g_m.time.mean),
            "Planning Time Std": float(g_m.time.std),
            "Planning Time Median": float(g_m.time.median),
            "Planning Time 75th": float(g_m.time.percent_75),
            "Planning Time 98th": float(g_m.time.percent_98),
        }
        write_yaml(kpi_data, join_path(args.save_path, args.file_name + ".yml"))

    # run on mb dataset:


def check_problems(all_problems):
    n_cube = 0
    for problem in all_problems:
        cache = (
            WorldConfig.from_dict(deepcopy(problem["obstacles"])).get_obb_world().get_cache_dict()
        )
        n_cube = max(n_cube, cache["obb"])
    return n_cube


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--save_path",
        type=str,
        default=".",
        help="path to save file",
    )
    parser.add_argument(
        "--file_name",
        type=str,
        default="mg_curobo_",
        help="File name prefix to use to save benchmark results",
    )
    parser.add_argument(
        "--collision_buffer",
        type=float,
        default=-0.00,  # in meters
        help="Robot collision buffer",
    )

    parser.add_argument(
        "--graph",
        action="store_true",
        help="When True, runs only geometric planner",
        default=False,
    )
    parser.add_argument(
        "--mesh",
        action="store_true",
        help="When True, converts obstacles to meshes",
        default=False,
    )
    parser.add_argument(
        "--kpi",
        action="store_true",
        help="When True, saves minimal metrics",
        default=False,
    )

    parser.add_argument(
        "--demo",
        action="store_true",
        help="When True, runs only on small dataaset",
        default=False,
    )
    parser.add_argument(
        "--disable_cuda_graph",
        action="store_true",
        help="When True, disable cuda graph during benchmarking",
        default=False,
    )
    parser.add_argument(
        "--write_benchmark",
        action="store_true",
        help="When True, writes paths to file",
        default=False,
    )

    args = parser.parse_args()

    setup_curobo_logger("error")
    benchmark_mb(
        save_log=False,
        write_usd=False,
        write_plot=False,
        write_benchmark=args.write_benchmark,
        plot_cost=False,
        save_kpi=args.kpi,
        graph_mode=args.graph,
        args=args,
    )
