# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This script demonstrates how to evaluate a pretrained policy from the HuggingFace Hub or from your local
training outputs directory. In the latter case, you might want to run examples/3_train_policy.py first.

It requires the installation of the 'gym_pusht' simulation environment. Install it by running:
```bash
pip install -e ".[pusht]"
```
"""

from pathlib import Path
import os

import gymnasium as gym
import mani_skill.envs
import imageio
import numpy as np
import torch
from scipy.spatial.transform import Rotation as R
from lerobot.common.policies.smolvla.modeling_smolvla import SmolVLAPolicy
from lerobot.common.datasets.lerobot_dataset import LeRobotDataset
from lerobot.common.constants import OBS_STATE
from CamHandler import PickCubeMultiCamEnv
from utils import *

# Create a directory to store the video of the evaluation
output_directory = Path("./videos")
output_directory.mkdir(parents=True, exist_ok=True)

# Select your device
os.environ["CUDA_VISIBLE_DEVICES"] = "0"
device = "cuda"

# Load Policy
pretrained_policy_path = "lerobot/smolvla_base"
dataset = LeRobotDataset("lerobot/svla_so101_pickplace", download_videos=False)
policy = SmolVLAPolicy.from_pretrained(pretrained_policy_path, dataset_stats=dataset.meta.stats)

# Initialize evaluation environment to render two observation types:
# an image of the scene and state/position of the agent. The environment
# also automatically stops running after 300 interactions/steps.

env = gym.make(
    "PickCubeMultiCam-v1",
    num_envs=1,
    obs_mode="state_dict+rgb",       # ← gives you a dict of images
    control_mode="pd_ee_delta_pose",
    render_mode="rgb_array",
    max_episode_steps=500
)

# We can verify that the shapes of the features expected by the policy match the ones from the observations
# produced by the environment
# print(f"Normalization mapping: {policy.config.normalization_mapping}")
# print(policy.config.input_features)
# print(env.observation_space)

# Similarly, we can check that the actions produced by the policy will match the actions expected by the
# environment
# print(policy.config.output_features)
# print(env.action_space)

# Reset the policy and environments to prepare for rollout
policy.reset()
raw_observation, info = env.reset(seed=42)
# print(f"Observation: {raw_observation}")

# Prepare to collect every rewards and all the frames of the episode,
# from initial state to final state.
rewards = []
frames = []

# Create output directory for videos
output_dir = Path("./videos")
output_dir.mkdir(parents=True, exist_ok=True)

# Prepare imageio writers for each camera
fps = env.metadata.get("render_fps", 30)
writers = {}
camera_uids = list(raw_observation["sensor_data"].keys())
for uid in camera_uids:
    video_path = output_dir / f"{uid}.mp4"
    writers[uid] = imageio.get_writer(str(video_path), fps=fps)
    print(f"Opened writer for {uid}: {video_path}")


step = 0
done = False
while not done:
    # Prepare observation for the policy running in Pytorch
    raw_pose = raw_observation["extra"]["tcp_pose"].numpy()
    state6 = convert_7_to_6(raw_pose)
    
    # to torch, float32, add batch dim → (1,6)
    state = torch.from_numpy(state6).to(torch.float32).unsqueeze(0)
    print(f"{step=} {raw_pose=}")
    
    top_image = raw_observation["sensor_data"]["top_camera"]["rgb"]      # Shape: [1, 256, 256, 3]
    side_image = raw_observation["sensor_data"]["side_camera"]["rgb"]
    wrist_image = raw_observation["sensor_data"]["wrist_camera"]["rgb"]

    # Convert to float32 with image from channel first in [0,255]
    # to channel last in [0,1]
    state = state.to(torch.float32)
    top_image = (top_image.to(torch.float32)
                 .permute(0, 3, 1, 2)
                 /255.0
                 )
    side_image = (side_image.to(torch.float32)
                 .permute(0, 3, 1, 2) 
                 /255.0
                 )
    wrist_image = (wrist_image.to(torch.float32)
                 .permute(0, 3, 1, 2) 
                 /255.0
                 )

    # Send data tensors from CPU to GPU
    state = state.to(device, non_blocking=True)
    top_image = top_image.to(device, non_blocking=True)
    side_image = side_image.to(device, non_blocking=True)
    wrist_image = wrist_image.to(device, non_blocking=True)

    # Add extra (empty) batch dimension, required to forward the policy
    # state = state.unsqueeze(0)
    # top_image = top_image.unsqueeze(0)
    # side_image = side_image.unsqueeze(0)
    # wrist_image = wrist_image.unsqueeze(0)

    # Create the policy input dictionary
    batch = {
        OBS_STATE: state,
        "observation.image2": top_image,
        "observation.image": wrist_image,
        "observation.image3": side_image,
        "task": "Pick up the red cube."
    }

    # Predict the next action with respect to the current observation
    with torch.inference_mode():
        action = policy.select_action(batch)
        action[:, :3] *= 0.05    # 5 cm per norm-unit
        action[:, 3:6] *= 0.2     # ~11° per norm-unit
        
        # Convert to 7-dimension action for env
        action = convert_6_to_7(action)
        
    # Prepare the action for the environment
    # action = action.squeeze(0).astype(np.float32)
    print(f"{step=} {action=}")

    # Step through the environment and receive a new observation
    raw_observation, reward, terminated, truncated, info = env.step(action)
    print(f"{step=} {reward=} {terminated=}")

    # Keep track of all the rewards and frames
    rewards.append(reward)
    # Append each camera frame to its writer
    for uid, data in raw_observation["sensor_data"].items():
        rgba = data["rgb"]           # tensor [1,H,W,4]
        rgb = rgba[0, ..., :3].cpu().numpy().astype(np.uint8)
        writers[uid].append_data(rgb)

    # The rollout is considered done when the success state is reached (i.e. terminated is True),
    # or the maximum number of iterations is reached (i.e. truncated is True)
    done = terminated | truncated | done
    step += 1

if terminated:
    print("Success!")
else:
    print("Failure!")

# Close all writers
for uid, writer in writers.items():
    writer.close()
    print(f"Saved video for camera '{uid}' at: {output_dir/ (uid + '.mp4')}" )