<!--
Copyright (c) 2023 NVIDIA CORPORATION & AFFILIATES. All rights reserved.

NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
property and proprietary rights in and to this material, related
documentation and any modifications thereto. Any use, reproduction,
disclosure or distribution of this material and related documentation
without an express license agreement from NVIDIA CORPORATION or
its affiliates is strictly prohibited.
-->
# CuRobo

*CUDA Accelerated Robot Library*

**Check [curobo.org](https://curobo.org) for installing and getting started with examples!**

Use [Discussions](https://github.com/NVlabs/curobo/discussions) for questions on using this package.

Use [Issues](https://github.com/NVlabs/curobo/issues) if you find a bug.


For business inquiries, please visit our website and submit the form: [NVIDIA Research Licensing](https://www.nvidia.com/en-us/research/inquiries/)

## Overview

CuRobo is a CUDA accelerated library containing a suite of robotics algorithms that run significantly faster than existing implementations leveraging parallel compute. CuRobo currently provides the following algorithms: (1) forward and inverse kinematics,
(2) collision checking between robot and world, with the world represented as Cuboids, Meshes, and Depth images, (3) numerical optimization with gradient descent, L-BFGS, and MPPI, (4) geometric planning, (5) trajectory optimization, (6) motion generation that combines inverse kinematics, geometric planning, and trajectory optimization to generate global motions within 30ms.

<p align="center">
<img width="500" src="images/robot_demo.gif">
</p>


CuRobo performs trajectory optimization across many seeds in parallel to find a solution. CuRobo's trajectory optimization penalizes jerk and accelerations, encouraging smoother and shorter trajectories. Below we compare CuRobo's motion generation on the left to a BiRRT planner on a pick and place task.

<p align="center">
<img width="500" src="images/rrt_compare.gif">
</p>

## Citation

If you found this work useful, please cite the below report,

```
@article{curobo_report23,
         title={CuRobo: Parallelized Collision-Free Minimum-Jerk Robot Motion Generation},
         author={Sundaralingam, Balakumar and Hari, Siva Kumar Sastry and 
         Fishman, Adam and Garrett, Caelan and Van Wyk, Karl and Blukis, Valts and 
         Millane, Alexander and Oleynikova, Helen and Handa, Ankur and 
         Ramos, Fabio and Ratliff, Nathan and Fox, Dieter},
         journal={arXiv preprint},
         year={2023}
}
```