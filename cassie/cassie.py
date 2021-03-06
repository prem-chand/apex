# Consolidated Cassie environment.

from .cassiemujoco import pd_in_t, state_out_t, CassieSim, CassieVis

from .trajectory import *
from cassie.quaternion_function import *
from .rewards import *

from math import floor

import numpy as np 
import os
import random
import copy

import pickle

import torch

# Load clock based reward functions from file
def load_reward_clock_funcs(path):
    with open(path, "rb") as f:
        clock_funcs = pickle.load(f)
    return clock_funcs


class CassieEnv_v2:
    def __init__(self, traj='walking', simrate=50, clock_based=True, state_est=True, dynamics_randomization=True, no_delta=True, learn_gains=False, ik_baseline=False, reward="iros_paper", history=0, **kwargs):

        dirname = os.path.dirname(__file__)
        #xml_path = os.path.join(dirname, "cassiemujoco", "cassie.xml")
        #self.sim = CassieSim(xml_path)
        self.sim = CassieSim("./cassie/cassiemujoco/cassie.xml")
        # self.sim = CassieSim("./cassie/cassiemujoco/cassie_drop_step.xml")
        self.vis = None

        # Arguments for the simulation and state space
        self.clock_based = clock_based
        self.state_est = state_est
        self.no_delta = no_delta
        self.dynamics_randomization = dynamics_randomization
        self.ik_baseline = ik_baseline

        # Arguments generally used for curriculum training
        self.reward_func = reward
        self.early_term_cutoff = 0.3
        curriculum_defaults = {
            "fixed_speed" : None
        }
        for (arg, default) in curriculum_defaults.items():
            setattr(self, arg, kwargs.get(arg, default))


        # CONFIGURE REF TRAJECTORY to use
        if traj == "aslip":
            self.speeds = np.array([x / 10 for x in range(0, 21)])
            self.trajectories = getAllTrajectories(self.speeds)
            self.num_speeds = len(self.trajectories)
            self.traj_idx = 0
            self.speed = self.speeds[self.traj_idx]
            self.trajectory = self.trajectories[self.traj_idx]
            self.aslip_traj = True
            self.clock_based = False
        else:
            self.aslip_traj = False
            if traj == "walking":
                traj_path = os.path.join(dirname, "trajectory", "stepdata.bin")
            elif traj == "stepping":
                traj_path = os.path.join(dirname, "trajectory", "more-poses-trial.bin")
            self.trajectory = CassieTrajectory(traj_path)
            self.speed = 0

        # Names of Clock-Based reward functions
        clock_rewards        =  ["clock_smooth", "clock_strict0.1", "clock_strict0.4",
                                 "clock_smooth_aerial", "clock_strict0.1_aerial", "clock_strict0.4_aerial"]
        aslip_clock_rewards   = ["aslip_clock_smooth", "aslip_clock_smooth_aerial",
                                 "aslip_clock_strict0.1", "aslip_clock_strict0.1_aerial",
                                 "aslip_clock_strict0.4", "aslip_clock_strict0.4_aerial"]
        max_vel_clock_rewards = ["max_vel_clock_smooth", "max_vel_clock_strict0.1", "max_vel_clock_strict0.4",
                                 "max_vel_clock_smooth_aerial", "max_vel_clock_strict0.1_aerial", "max_vel_clock_strict0.4_aerial"]

        # If Loading Clock-based Reward Func, do that
        if self.reward_func in clock_rewards:
            # TODO: Get dedicated reward funcs for clock reward
            clock_funcs_path = os.path.join(dirname, "rewards", "reward_clock_funcs", "aslip_" + self.reward_func + ".pkl")
            self.reward_clock_funcs = load_reward_clock_funcs(clock_funcs_path)
            self.left_clock = self.reward_clock_funcs["left"][-1]
            self.right_clock = self.reward_clock_funcs["right"][-1]
            self.reward_func = "clock"
        elif self.reward_func in aslip_clock_rewards:
            clock_funcs_path = os.path.join(dirname, "rewards", "reward_clock_funcs", self.reward_func + ".pkl")
            self.reward_clock_funcs = load_reward_clock_funcs(clock_funcs_path)
            self.left_clock = self.reward_clock_funcs["left"][self.traj_idx]
            self.right_clock = self.reward_clock_funcs["right"][self.traj_idx]
            self.reward_func = "aslip_clock"
        elif self.reward_func in max_vel_clock_rewards:
            # TODO: Get dedicated reward funcs for clock reward
            clock_funcs_path = os.path.join(dirname, "rewards", "reward_clock_funcs", "aslip_" + self.reward_func[8:] + ".pkl")
            self.reward_clock_funcs = load_reward_clock_funcs(clock_funcs_path)
            self.left_clock = self.reward_clock_funcs["left"][-1]
            self.right_clock = self.reward_clock_funcs["right"][-1]
            self.reward_func = "max_vel_clock"

        self.observation_space, self.clock_inds, self.mirrored_obs = self.set_up_state_space()

        # Adds option for state history for FF nets
        self._obs = len(self.observation_space)
        self.history = history

        self.observation_space = np.zeros(self._obs + self._obs * self.history)

        self.P = np.array([100,  100,  88,  96,  50]) 
        self.D = np.array([10.0, 10.0, 8.0, 9.6, 5.0])

        # learn gains means there is a delta on the default PD gains ***FOR EACH LEG***
        self.learn_gains = learn_gains
        if self.learn_gains == True:
            self.action_space = np.zeros(10 + 20)
        else:
            self.action_space = np.zeros(10)


        self.u = pd_in_t()

        # TODO: should probably initialize this to current state
        self.cassie_state = state_out_t()

        self.simrate = simrate # simulate X mujoco steps with same pd target
                                # 50 brings simulation from 2000Hz to exactly 40Hz

        self.simsteps = 0 # number of simulation steps

        self.time    = 0 # number of time steps in current episode
        self.phase   = 0 # portion of the phase the robot is in
        self.counter = 0 # number of phase cycles completed in episode

        # NOTE: a reference trajectory represents ONE phase cycle

        # should be floor(len(traj) / simrate) - 1
        # should be VERY cautious here because wrapping around trajectory
        # badly can cause assymetrical/bad gaits
        self.phaselen = floor(len(self.trajectory) / self.simrate) - 1 if not self.aslip_traj else self.trajectory.length - 1

        # see include/cassiemujoco.h for meaning of these indices
        self.pos_idx = [7, 8, 9, 14, 20, 21, 22, 23, 28, 34]
        self.vel_idx = [6, 7, 8, 12, 18, 19, 20, 21, 25, 31]

        self.pos_index = np.array([1,2,3,4,5,6,7,8,9,14,15,16,20,21,22,23,28,29,30,34])
        self.vel_index = np.array([0,1,2,3,4,5,6,7,8,12,13,14,18,19,20,21,25,26,27,31])

        # CONFIGURE OFFSET for No Delta Policies
        if self.aslip_traj:
            ref_pos, ref_vel = self.get_ref_state(self.phase)
            self.offset = ref_pos[self.pos_idx]
        else:
            self.offset = np.array([0.0045, 0.0, 0.4973, -1.1997, -1.5968, 0.0045, 0.0, 0.4973, -1.1997, -1.5968])

        self.phase_add = 1

        # global flat foot orientation, can be useful part of reward function:
        self.neutral_foot_orient = np.array([-0.24790886454547323, -0.24679713195445646, -0.6609396704367185, 0.663921021343526])
        
        # tracking various variables for reward funcs
        self.stepcount = 0
        self.l_high = False  # only true if foot is above 0.2m 
        self.r_high = False
        self.l_swing = False  # these will be true even if foot is barely above ground
        self.r_swing = False
        self.l_foot_frc = 0
        self.r_foot_frc = 0
        self.l_foot_vel = np.zeros(3)
        self.r_foot_vel = np.zeros(3)
        self.l_foot_pos = np.zeros(3)
        self.r_foot_pos = np.zeros(3)
        self.l_foot_orient = 0
        self.r_foot_orient = 0

        # TODO: should this be mujoco tracking var or use state estimator. real command interface will use state est
        # Track pelvis position as baseline for pelvis tracking command inputs
        self.last_pelvis_pos = self.sim.qpos()[0:3]

        #### Dynamics Randomization ####
        self.dynamics_randomization = dynamics_randomization
        self.slope_rand = dynamics_randomization
        self.joint_rand = dynamics_randomization
        # Record default dynamics parameters
        if self.dynamics_randomization:
            self.default_damping = self.sim.get_dof_damping()
            self.default_mass = self.sim.get_body_mass()
            self.default_ipos = self.sim.get_body_ipos()
            self.default_fric = self.sim.get_geom_friction()

            weak_factor = 0.5
            strong_factor = 0.5

            pelvis_damp_range = [[self.default_damping[0], self.default_damping[0]], 
                                [self.default_damping[1], self.default_damping[1]], 
                                [self.default_damping[2], self.default_damping[2]], 
                                [self.default_damping[3], self.default_damping[3]], 
                                [self.default_damping[4], self.default_damping[4]], 
                                [self.default_damping[5], self.default_damping[5]]] 

            hip_damp_range = [[self.default_damping[6]*weak_factor, self.default_damping[6]*strong_factor],
                            [self.default_damping[7]*weak_factor, self.default_damping[7]*strong_factor],
                            [self.default_damping[8]*weak_factor, self.default_damping[8]*strong_factor]]  # 6->8 and 19->21

            achilles_damp_range = [[self.default_damping[9]*weak_factor,  self.default_damping[9]*strong_factor],
                                    [self.default_damping[10]*weak_factor, self.default_damping[10]*strong_factor], 
                                    [self.default_damping[11]*weak_factor, self.default_damping[11]*strong_factor]] # 9->11 and 22->24

            knee_damp_range     = [[self.default_damping[12]*weak_factor, self.default_damping[12]*strong_factor]]   # 12 and 25
            shin_damp_range     = [[self.default_damping[13]*weak_factor, self.default_damping[13]*strong_factor]]   # 13 and 26
            tarsus_damp_range   = [[self.default_damping[14], self.default_damping[14]]]             # 14 and 27
            heel_damp_range     = [[self.default_damping[15], self.default_damping[15]]]                           # 15 and 28
            fcrank_damp_range   = [[self.default_damping[16]*weak_factor, self.default_damping[16]*strong_factor]]   # 16 and 29
            prod_damp_range     = [[self.default_damping[17], self.default_damping[17]]]                           # 17 and 30
            foot_damp_range     = [[self.default_damping[18]*weak_factor, self.default_damping[18]*strong_factor]]   # 18 and 31

            side_damp = hip_damp_range + achilles_damp_range + knee_damp_range + shin_damp_range + tarsus_damp_range + heel_damp_range + fcrank_damp_range + prod_damp_range + foot_damp_range
            self.damp_range = pelvis_damp_range + side_damp + side_damp

            hi = 1.1
            lo = 0.9
            m = self.default_mass
            pelvis_mass_range      = [[lo*m[1],  hi*m[1]]]  # 1
            hip_mass_range         = [[lo*m[2],  hi*m[2]],  # 2->4 and 14->16
                                    [lo*m[3],  hi*m[3]], 
                                    [lo*m[4],  hi*m[4]]] 

            achilles_mass_range    = [[lo*m[5],  hi*m[5]]]  # 5 and 17
            knee_mass_range        = [[lo*m[6],  hi*m[6]]]  # 6 and 18
            knee_spring_mass_range = [[lo*m[7],  hi*m[7]]]  # 7 and 19
            shin_mass_range        = [[lo*m[8],  hi*m[8]]]  # 8 and 20
            tarsus_mass_range      = [[lo*m[9],  hi*m[9]]]  # 9 and 21
            heel_spring_mass_range = [[lo*m[10], hi*m[10]]] # 10 and 22
            fcrank_mass_range      = [[lo*m[11], hi*m[11]]] # 11 and 23
            prod_mass_range        = [[lo*m[12], hi*m[12]]] # 12 and 24
            foot_mass_range        = [[lo*m[13], hi*m[13]]] # 13 and 25

            side_mass = hip_mass_range + achilles_mass_range \
                        + knee_mass_range + knee_spring_mass_range \
                        + shin_mass_range + tarsus_mass_range \
                        + heel_spring_mass_range + fcrank_mass_range \
                        + prod_mass_range + foot_mass_range

            self.mass_range = [[0, 0]] + pelvis_mass_range + side_mass + side_mass

        # self.sim.set_geom_friction([0.6, 1e-4, 5e-5], "floor")
        # self.delta_x_min, self.delta_x_max = self.default_ipos[3] - 0.05, self.default_ipos[3] + 0.05
        # self.delta_y_min, self.delta_y_max = self.default_ipos[4] - 0.05, self.default_ipos[4] + 0.05

        ### Trims ###
        self.joint_offsets = np.zeros(16)
        self.com_vel_offset = 0
        self.y_offset = 0

        ### Random commands during training ###
        self.speed_schedule = np.zeros(4)
        self.orient_add = 0
        self.orient_time = 500

        # Keep track of actions, torques
        self.prev_action = np.zeros(10)
        self.curr_action = None
        self.prev_torque = None

        # for RNN policies
        self.critic_state = None

        self.debug = False

    def set_up_state_space(self):

        mjstate_size   = 40
        state_est_size = 46

        speed_size     = 1

        clock_size    = 2
        
        # Find the mirrored trajectory
        if self.aslip_traj:
            ref_traj_size = 18
            mirrored_traj = np.array([6,7,8,9,10,11,0.1,1,2,3,4,5,12,13,14,15,16,17])
        else:
            ref_traj_size = 40
            mirrored_traj = np.array([0.1, 1, 2, 3, 4, 5, -13, -14, 15, 16, 17, 18, 19, -6, -7, 8, 9, 10, 11, 12,
                                        20, 21, 22, 23, 24, 25, -33, -34, 35, 36, 37, 38, 39, -26, -27, 28, 29, 30, 31, 32])
            
        if self.state_est:
            base_mir_obs = np.array([0.1, 1, -2, 3, -4, -10, -11, 12, 13, 14, -5, -6, 7, 8, 9, 15, -16, 17, -18, 19, -20, -26, -27, 28, 29, 30, -21, -22, 23, 24, 25, 31, -32, 33, 37, 38, 39, 34, 35, 36, 43, 44, 45, 40, 41, 42])
            obs_size = state_est_size
        else:
            base_mir_obs = np.array([0.1, 1, 2, -3, 4, -5, -13, -14, 15, 16, 17, 18, 19, -6, -7, 8, 9, 10, 11, 12, 20, -21, 22, -23, 24, -25, -33, -34, 35, 36, 37, 38, 39, -26, -27, 28, 29, 30, 31, 32])
            obs_size = mjstate_size
        if self.clock_based:
            append_obs = np.array([len(base_mir_obs) + i for i in range(clock_size+speed_size)])
            mirrored_obs = np.concatenate([base_mir_obs, append_obs])
            clock_inds = append_obs[0:clock_size].tolist()
            obs_size += clock_size + speed_size
        else:
            mirrored_traj_sign = np.multiply(np.sign(mirrored_traj), obs_size+np.floor(np.abs(mirrored_traj)))
            mirrored_obs = np.concatenate([base_mir_obs, mirrored_traj_sign])
            clock_inds = None
            obs_size += ref_traj_size

        observation_space = np.zeros(obs_size)
        mirrored_obs = mirrored_obs.tolist()

        # check_arr = np.arange(obs_size, dtype=np.float64)
        # check_arr[0] = 0.1
        # print("mir obs check: ", np.all(np.sort(np.abs(mirrored_obs)) == check_arr))
        # exit()

        return observation_space, clock_inds, mirrored_obs

    def step_simulation(self, action, learned_gains=None):

        if not self.ik_baseline:
            if self.aslip_traj and self.phase == self.phaselen - 1:
                ref_pos, ref_vel = self.get_ref_state(0)
            else:
                ref_pos, ref_vel = self.get_ref_state(self.phase + self.phase_add)
        else:
            ref_pos = self.trajectory.ik_pos[self.simsteps]

        if not self.no_delta:
            self.offset = ref_pos[self.pos_idx]
        target = action + self.offset

        if self.joint_rand:
            target -= self.joint_offsets[0:10]

        foot_pos = np.zeros(6)
        self.sim.foot_pos(foot_pos)
        prev_foot = copy.deepcopy(foot_pos)
        self.u = pd_in_t()
        for i in range(5):

            # TODO: move setting gains out of the loop?
            # maybe write a wrapper for pd_in_t ?
            if self.learn_gains == False:                
                self.u.leftLeg.motorPd.pGain[i]  = self.P[i]
                self.u.rightLeg.motorPd.pGain[i] = self.P[i]
                self.u.leftLeg.motorPd.dGain[i]  = self.D[i]
                self.u.rightLeg.motorPd.dGain[i] = self.D[i]
            else:
                self.u.leftLeg.motorPd.pGain[i]  = self.P[i] + learned_gains[i]
                self.u.rightLeg.motorPd.pGain[i] = self.P[i] + learned_gains[5+i]
                self.u.leftLeg.motorPd.dGain[i]  = self.D[i] + learned_gains[10+i]
                self.u.rightLeg.motorPd.dGain[i] = self.D[i] + learned_gains[15+i]

            self.u.leftLeg.motorPd.torque[i]  = 0 # Feedforward torque
            self.u.rightLeg.motorPd.torque[i] = 0 

            self.u.leftLeg.motorPd.pTarget[i]  = target[i]
            self.u.rightLeg.motorPd.pTarget[i] = target[i + 5]

            self.u.leftLeg.motorPd.dTarget[i]  = 0
            self.u.rightLeg.motorPd.dTarget[i] = 0

        self.cassie_state = self.sim.step_pd(self.u)
        self.sim.foot_pos(foot_pos)
        self.l_foot_vel = (foot_pos[0:3] - prev_foot[0:3]) / 0.0005
        self.r_foot_vel = (foot_pos[3:6] - prev_foot[3:6]) / 0.0005
        foot_forces = self.sim.get_foot_forces()
        if self.l_high and foot_forces[0] > 0:
            self.l_high = False
            self.stepcount += 1
        elif not self.l_high and foot_pos[2] >= 0.2:
            self.l_high = True
        if self.r_high and foot_forces[0] > 0:
            self.stepcount += 1
            self.r_high = False
        elif not self.r_high and foot_pos[5] >= 0.2:
            self.r_high = True

        if self.l_swing and foot_forces[0] > 0:
            self.l_swing = False
        elif not self.l_swing and foot_pos[2] >= 0:
            self.l_swing = True
        if self.r_swing and foot_forces[0] > 0:
            self.r_swing = False
        elif not self.r_swing and foot_pos[5] >= 0:
            self.r_swing = True

    def step(self, action, return_omniscient_state=False):
        
        # reset mujoco tracking variables
        self.l_foot_frc = 0
        self.r_foot_frc = 0
        foot_pos = np.zeros(6)
        self.l_foot_pos = np.zeros(3)
        self.r_foot_pos = np.zeros(3)
        self.l_foot_orient_cost = 0
        self.r_foot_orient_cost = 0

        if self.learn_gains:
            action, learned_gains = action[0:10], action[10:]

        for i in range(self.simrate):
            if self.learn_gains:
                self.step_simulation(action, learned_gains)
            else:
                self.step_simulation(action)
            self.simsteps += 1
            qpos = np.copy(self.sim.qpos())
            qvel = np.copy(self.sim.qvel())
            # Foot Force Tracking
            foot_forces = self.sim.get_foot_forces()
            self.l_foot_frc += foot_forces[0]
            self.r_foot_frc += foot_forces[1]
            # Relative Foot Position tracking
            self.sim.foot_pos(foot_pos)
            self.l_foot_pos += foot_pos[0:3]
            self.r_foot_pos += foot_pos[3:6]
            # Foot Orientation Cost
            self.l_foot_orient_cost += (1 - np.inner(self.neutral_foot_orient, self.sim.xquat("left-foot")) ** 2)
            self.r_foot_orient_cost += (1 - np.inner(self.neutral_foot_orient, self.sim.xquat("right-foot")) ** 2)
        
        self.l_foot_frc              /= self.simrate
        self.r_foot_frc              /= self.simrate        
        self.l_foot_pos              /= self.simrate
        self.r_foot_pos              /= self.simrate
        self.l_foot_orient_cost      /= self.simrate
        self.r_foot_orient_cost      /= self.simrate

        height = self.sim.qpos()[2]
        self.curr_action = action

        self.time  += 1
        self.phase += self.phase_add

        if (self.aslip_traj and self.phase >= self.phaselen) or self.phase > self.phaselen:
            self.last_pelvis_pos = self.sim.qpos()[0:3]
            self.simsteps = 0
            self.phase = 0
            self.counter += 1

        # no more knee walking
        if self.sim.xpos("left-tarsus")[2] < 0.1 or self.sim.xpos("right-tarsus")[2] < 0.1:
            done = True
            # print("left tarsus: {:.2f}\tleft foot: {:.2f}".format(self.sim.xpos("left-tarsus")[2], self.sim.xpos("left-foot")[2]))
            # print("right tarsus: {:.2f}\tright foot: {:.2f}".format(self.sim.xpos("right-tarsus")[2], self.sim.xpos("right-foot")[2]))
            # while(1):
            #     self.vis.draw(self.sim)
        elif height < 0.4 or height > 3.0:
            done = True
        else:
            done = False

        reward = self.compute_reward(action)

        # update previous action
        self.prev_action = action

        # TODO: make 0.3 a variable/more transparent
        if reward < self.early_term_cutoff:
            done = True

        if return_omniscient_state:
            return self.get_full_state(), self.get_omniscient_state(), reward, done, {}
        else:
            return self.get_full_state(), reward, done, {}

    def reset(self):

        if self.aslip_traj:
            self.traj_idx = random.randint(0, self.num_speeds-1)
            self.speed = self.speeds[self.traj_idx]
            # print("current speed: {}\tcurrent traj: {}".format(self.speed, random_speed_idx))
            self.trajectory = self.trajectories[self.traj_idx] # switch the current trajectory
            self.phaselen = self.trajectory.length - 1
        else:
            self.speed = (random.randint(0, 20)) / 10
            # # Make sure that if speed is above 2, freq is at least 1.2
            # if self.speed > 1.3:# or np.any(self.speed_schedule > 1.6):
            #     self.phase_add = 1.3 + 0.7*random.random()
            # else:
            #     self.phase_add = 1 + random.random()

        if self.fixed_speed != None:
            self.traj_idx = (np.abs(self.speeds - self.speed)).argmin()
            self.speed = self.fixed_speed
        
        if self.reward_func == "clock":
            self.left_clock = self.reward_clock_funcs["left"][-1]
            self.right_clock = self.reward_clock_funcs["right"][-1]
        elif self.reward_func == "aslip_clock":
            self.left_clock = self.reward_clock_funcs["left"][self.traj_idx]
            self.right_clock = self.reward_clock_funcs["right"][self.traj_idx]
        elif self.reward_func == "max_vel_clock":
            self.left_clock = self.reward_clock_funcs["left"][-1]
            self.right_clock = self.reward_clock_funcs["right"][-1]

        self.simsteps = 0

        self.phase = random.randint(0, self.phaselen)
        self.time = 0
        self.counter = 0

        self.state_history = [np.zeros(self._obs) for _ in range(self.history+1)]

        qpos, qvel = self.get_ref_state(self.phase)
        # orientation = random.randint(-10, 10) * np.pi / 25
        # quaternion = euler2quat(z=orientation, y=0, x=0)
        # qpos[3:7] = quaternion
        # self.y_offset = 0#random.uniform(-3.5, 3.5)
        # qpos[1] = self.y_offset

        if self.aslip_traj:
            qvel = np.zeros(qvel.shape)

        self.sim.set_qpos(qpos)
        self.sim.set_qvel(qvel)

        self.last_pelvis_pos = self.sim.qpos()[0:3]

        # Need to reset u? Or better way to reset cassie_state than taking step
        self.cassie_state = self.sim.step_pd(self.u)

        self.orient_add = 0#random.randint(-10, 10) * np.pi / 25
        self.orient_time = 0#random.randint(50, 200) 
        self.com_vel_offset = 0#0.1*np.random.uniform(-0.1, 0.1, 2)

        # reset mujoco tracking variables
        self.l_foot_frc = 0
        self.r_foot_frc = 0
        self.l_foot_orient_cost = 0
        self.r_foot_orient_cost = 0

        if self.dynamics_randomization:
            #### Dynamics Randomization ####
            # damp_noise = [np.random.uniform(a, b) for a, b in self.damp_range]
            # mass_noise = [np.random.uniform(a, b) for a, b in self.mass_range]
            # com_noise = [0, 0, 0] + [np.random.uniform(self.delta_x_min, self.delta_x_min)] + [np.random.uniform(self.delta_y_min, self.delta_y_max)] + [0] + list(self.default_ipos[6:])
            # fric_noise = [np.random.uniform(0.95, 1.05)] + [np.random.uniform(5e-4, 5e-3)] + [np.random.uniform(5e-5, 5e-4)]#+ list(self.default_fric[2:])
            # fric_noise = []
            # translational = np.random.uniform(0.6, 1.2)
            # torsional = np.random.uniform(1e-4, 1e-2)
            # rolling = np.random.uniform(5e-5, 5e-4)
            # for _ in range(int(len(self.default_fric)/3)):
            #     fric_noise += [translational, torsional, rolling]
            fric_noise = [np.random.uniform(0.6, 1.2), np.random.uniform(1e-4, 1e-2), np.random.uniform(5e-5, 5e-4)]
            # self.sim.set_dof_damping(np.clip(damp_noise, 0, None))
            # self.sim.set_body_mass(np.clip(mass_noise, 0, None))
            # self.sim.set_body_ipos(com_noise)
            self.sim.set_geom_friction(np.clip(fric_noise, 0, None), "floor")
            self.sim.set_const()

        if self.slope_rand:
            rand_angle = np.pi/180*np.random.uniform(-5, 5, 2)
            floor_quat = euler2quat(z=0, y=rand_angle[0], x=rand_angle[1])
            self.sim.set_geom_quat(floor_quat, "floor")
        if self.joint_rand:
            self.joint_offsets = np.random.uniform(-0.03, 0.03, 16)
            # Set motor and joint foot to be same offset
            self.joint_offsets[4] = self.joint_offsets[12]
            self.joint_offsets[9] = self.joint_offsets[15]

        return self.get_full_state()

    def reset_for_test(self, full_reset=False):
        self.simsteps = 0
        self.phase = 0
        self.time = 0
        self.counter = 0
        self.orient_add = 0
        self.orient_time = np.inf
        self.y_offset = 0
        self.phase_add = 1

        self.state_history = [np.zeros(self._obs) for _ in range(self.history+1)]

        if self.aslip_traj:
            self.traj_idx = 0
            self.speed = 0
            # print("current speed: {}".format(self.speed))
            self.trajectory = self.trajectories[self.traj_idx] # switch the current trajectory
            self.phaselen = self.trajectory.length - 1
        else:
            self.speed = 0

        if self.reward_func == "aslip_clock":
            self.left_clock = self.reward_clock_funcs["left"][self.traj_idx]
            self.right_clock = self.reward_clock_funcs["right"][self.traj_idx]

        if not full_reset:
            qpos, qvel = self.get_ref_state(self.phase)
            self.sim.set_qpos(qpos)
            self.sim.set_qvel(qvel)

            # reset mujoco tracking variables
            self.last_pelvis_pos = self.sim.qpos()[0:3]
            self.l_foot_frc = 0
            self.r_foot_frc = 0
            self.l_foot_orient = 0
            self.r_foot_orient = 0

            # Need to reset u? Or better way to reset cassie_state than taking step
            self.cassie_state = self.sim.step_pd(self.u)
        else:
            self.sim.full_reset()
            self.reset_cassie_state()

        if self.dynamics_randomization:
            self.sim.set_dof_damping(self.default_damping)
            self.sim.set_body_mass(self.default_mass)
            # self.sim.set_body_ipos(self.default_ipos)
            self.sim.set_geom_friction(self.default_fric)
            self.sim.set_const()

        if self.slope_rand:
            self.sim.set_geom_quat(np.array([1, 0, 0, 0]), "floor")

        return self.get_full_state()

    def reset_cassie_state(self):
        # Only reset parts of cassie_state that is used in get_full_state
        self.cassie_state.pelvis.position[:] = [0, 0, 1.01]
        self.cassie_state.pelvis.orientation[:] = [1, 0, 0, 0]
        self.cassie_state.pelvis.rotationalVelocity[:] = np.zeros(3)
        self.cassie_state.pelvis.translationalVelocity[:] = np.zeros(3)
        self.cassie_state.pelvis.translationalAcceleration[:] = np.zeros(3)
        self.cassie_state.terrain.height = 0
        self.cassie_state.motor.position[:] = [0.0045, 0, 0.4973, -1.1997, -1.5968, 0.0045, 0, 0.4973, -1.1997, -1.5968]
        self.cassie_state.motor.velocity[:] = np.zeros(10)
        self.cassie_state.joint.position[:] = [0, 1.4267, -1.5968, 0, 1.4267, -1.5968]
        self.cassie_state.joint.velocity[:] = np.zeros(6)

    # Helper function for updating the speed, used in visualization tests
    # not needed in training cause we don't change speeds in middle of rollout, and 
    # we randomize the starting phase of each rollout
    def update_speed(self, new_speed):
        if self.aslip_traj:
            self.traj_idx = (np.abs(self.speeds - new_speed)).argmin()
            self.speed = self.traj_idx / 10
            self.trajectory = self.trajectories[self.traj_idx]
            old_phaselen = self.phaselen
            self.phaselen = self.trajectory.length - 1
            # update phase
            self.phase = int(self.phaselen * self.phase / old_phaselen)
            # new offset
            ref_pos, ref_vel = self.get_ref_state(self.phase)
            self.offset = ref_pos[self.pos_idx]
        else:
            self.speed = new_speed
        
            # # Make sure that if speed is above 2, freq is at least 1.2
            # if self.speed > 1.3:# or np.any(self.speed_schedule > 1.6):
            #     self.phase_add = 1.1 + (self.speed - 1)* 0.2
        
        if self.reward_func == "aslip_clock":
            self.left_clock = self.reward_clock_funcs["left"][self.traj_idx]
            self.right_clock = self.reward_clock_funcs["right"][self.traj_idx]

    def compute_reward(self, action):
        qpos = np.copy(self.sim.qpos())
        qvel = np.copy(self.sim.qvel())

        ref_pos, ref_vel = self.get_ref_state(self.phase)
        if self.reward_func == "clock":
            self.early_term_cutoff = 0.2
            return clock_reward(self, action)
        elif self.reward_func == "max_vel_clock":
            self.early_term_cutoff = 0.2
            return max_vel_clock_reward(self, action)
        elif self.reward_func == "aslip_clock":
            self.early_term_cutoff = 0.2
            return aslip_clock_reward(self, action)
        elif self.reward_func == "aslip_old":
            self.early_term_cutoff = 0.0
            return aslip_old_reward(self, action)      
        elif self.reward_func == "iros_paper":
            return iros_paper_reward(self)
        elif self.reward_func == "5k_speed_reward":
            return old_speed_reward(self)
        else:
            raise NotImplementedError

  # get the corresponding state from the reference trajectory for the current phase
    def get_ref_state(self, phase=None):
        if phase is None:
            phase = self.phase

        if phase > self.phaselen:
            phase = 0

        desired_ind = phase * self.simrate if not self.aslip_traj else phase
        # phase_diff = desired_ind - math.floor(desired_ind)
        # if phase_diff != 0:       # desired ind is an int
        #     pos_prev = np.copy(self.trajectory.qpos[math.floor(desired_ind)])
        #     vel_prev = np.copy(self.trajectory.qvel[math.floor(desired_ind)])
        #     pos_next = np.copy(self.trajectory.qpos[math.ceil(desired_ind)])
        #     vel_next = np.copy(self.trajectory.qvel[math.ceil(desired_ind)])
        #     pos = pos_prev + phase_diff * (pos_next - pos_prev)
        #     vel = vel_prev + phase_diff * (vel_next - vel_prev)
        # else:
        # print("desired ind: ", desired_ind)
        pos = np.copy(self.trajectory.qpos[int(desired_ind)])
        vel = np.copy(self.trajectory.qvel[int(desired_ind)])

        # this is just setting the x to where it "should" be given the number
        # of cycles
        # pos[0] += (self.trajectory.qpos[-1, 0] - self.trajectory.qpos[0, 0]) * self.counter
        
        # ^ should only matter for COM error calculation,
        # gets dropped out of state variable for input reasons

        ###### Setting variable speed  #########
        if not self.aslip_traj:
            pos[0] *= self.speed
            pos[0] += (self.trajectory.qpos[-1, 0] - self.trajectory.qpos[0, 0]) * self.counter * self.speed
        else:
            pos[0] += (self.trajectory.qpos[-1, 0] - self.trajectory.qpos[0, 0]) * self.counter
        ######                          ########

        # setting lateral distance target to 0?
        # regardless of reference trajectory?
        pos[1] = 0

        if not self.aslip_traj:
            vel[0] *= self.speed

        return pos, vel

    def get_full_state(self):
        qpos = np.copy(self.sim.qpos())
        qvel = np.copy(self.sim.qvel()) 

        ref_pos, ref_vel = self.get_ref_state(self.phase + self.phase_add)

        # TODO: maybe convert to set subtraction for clarity
        # {i for i in range(35)} - 
        # {0, 10, 11, 12, 13, 17, 18, 19, 24, 25, 26, 27, 31, 32, 33}

        # this is everything except pelvis x and qw, achilles rod quaternions, 
        # and heel spring/foot crank/plantar rod angles
        # note: x is forward dist, y is lateral dist, z is height

        # makes sense to always exclude x because it is in global coordinates and
        # irrelevant to phase-based control. Z is inherently invariant to
        # trajectory despite being global coord. Y is only invariant to straight
        # line trajectories.

        # CLOCK BASED (NO TRAJECTORY)
        if self.clock_based:
            clock = [np.sin(2 * np.pi *  self.phase / self.phaselen),
                    np.cos(2 * np.pi *  self.phase / self.phaselen)]
            
            ext_state = np.concatenate((clock, [self.speed]))

        # ASLIP TRAJECTORY
        elif self.aslip_traj and not self.clock_based:
            if(self.phase == 0):
                # ext_state = np.concatenate(get_ref_aslip_ext_state(self, self.cassie_state, self.last_pelvis_pos, self.phaselen - 1, offset=self.vertOffset))
                ext_state = np.concatenate(get_ref_aslip_unaltered_state(self, self.phaselen - 1))
            else:
                # ext_state = np.concatenate(get_ref_aslip_ext_state(self, self.cassie_state, self.last_pelvis_pos, self.phaselen - 1, offset=self.vertOffset))
                ext_state = np.concatenate(get_ref_aslip_unaltered_state(self, self.phase))

        # OTHER TRAJECTORY
        else:
            ext_state = np.concatenate([ref_pos[self.pos_index], ref_vel[self.vel_index]])

        # Update orientation
        new_orient = self.cassie_state.pelvis.orientation[:]
        new_translationalVelocity = self.cassie_state.pelvis.translationalVelocity[:]
        new_translationalAcceleleration = self.cassie_state.pelvis.translationalAcceleration[:]
        # new_translationalVelocity[0:2] += self.com_vel_offset
        quaternion = euler2quat(z=self.orient_add, y=0, x=0)
        iquaternion = inverse_quaternion(quaternion)
        new_orient = quaternion_product(iquaternion, self.cassie_state.pelvis.orientation[:])
        if new_orient[0] < 0:
            new_orient = -new_orient
        new_translationalVelocity = rotate_by_quaternion(self.cassie_state.pelvis.translationalVelocity[:], iquaternion)
        new_translationalAcceleleration = rotate_by_quaternion(self.cassie_state.pelvis.translationalAcceleration[:], iquaternion)
        motor_pos = self.cassie_state.motor.position[:]
        joint_pos = self.cassie_state.joint.position[:]
        if self.joint_rand:
            motor_pos += self.joint_offsets[0:10]
            joint_pos += self.joint_offsets[10:16]

        # Use state estimator
        robot_state = np.concatenate([
            [self.cassie_state.pelvis.position[2] - self.cassie_state.terrain.height], # pelvis height
            new_orient,                                 # pelvis orientation
            motor_pos,                                     # actuated joint positions

            new_translationalVelocity,                       # pelvis translational velocity
            self.cassie_state.pelvis.rotationalVelocity[:],                          # pelvis rotational velocity 
            self.cassie_state.motor.velocity[:],                                     # actuated joint velocities

            new_translationalAcceleleration,                   # pelvis translational acceleration
            
            joint_pos,                                     # unactuated joint positions
            self.cassie_state.joint.velocity[:]                                      # unactuated joint velocities
        ])

        if self.state_est:
            state = np.concatenate([robot_state, ext_state])
        else:
            state = np.concatenate([qpos[self.pos_index], qvel[self.vel_index], ext_state])

        self.state_history.insert(0, state)
        self.state_history = self.state_history[:self.history+1]

        return np.concatenate(self.state_history)

    def render(self):
        if self.vis is None:
            self.vis = CassieVis(self.sim, "./cassie/cassiemujoco/cassie.xml")

        return self.vis.draw(self.sim)
    
    def get_state_info(self):
        # state estimator info
        pelvis_pos = self.cassie_state.pelvis.position[:]
        pelvis_pos[2] = pelvis_pos[2] - self.cassie_state.terrain.height
        lf_pos = self.cassie_state.leftFoot.position[:]
        rf_pos = self.cassie_state.rightFoot.position[:]
        lf_pos_global = [pelvis_pos[i] + lf_pos[i] for i in range(3)]
        rf_pos_global = [pelvis_pos[i] + rf_pos[i] for i in range(3)]
        # robot_state_info = np.array([pelvis_pos, lf_pos, rf_pos])
        robot_state_info = np.array([pelvis_pos, lf_pos_global, rf_pos_global])
        
        # mujoco info
        qpos = self.sim.qpos()
        actual_compos = qpos[0:3]
        actual_lf = self.l_foot_pos
        actual_rf = self.r_foot_pos
        # actual_lf = self.l_foot_pos - qpos[0:3]
        # actual_rf = self.r_foot_pos - qpos[0:3]
        actual_state_info = np.array([actual_compos, actual_lf, actual_rf])

        return robot_state_info, actual_state_info

    # This is assumed to be called after env.step. hence, using self.phase - 1 
    def get_traj_and_state_info(self):
        # traj info used in rewards
        traj_info = get_ref_aslip_global_state(self, self.phase-1)
        # traj_info = get_ref_aslip_unaltered_state(self, self.phase)
        traj_info = [traj_info[4], traj_info[2], traj_info[0]]
        
        # traj info going into the policy
        # traj_cmd_info = get_ref_aslip_ext_state(self, self.cassie_state, self.last_pelvis_pos, self.phase, offset=self.vertOffset)
        traj_cmd_info = get_ref_aslip_unaltered_state(self, self.phase-1)
        traj_cmd_info = [traj_cmd_info[4], traj_cmd_info[2], traj_cmd_info[0]]
        
        robot_state_info, actual_state_info = self.get_state_info()

        return traj_info, traj_cmd_info, robot_state_info, actual_state_info


# Currently unused
# def get_omniscient_state(self):
#     full_state = self.get_full_state()
#     omniscient_state = np.hstack((full_state, self.sim.get_dof_damping(), self.sim.get_body_mass(), self.sim.get_body_ipos(), self.sim.get_ground_friction))
#     return omniscient_state

#nbody layout:
# 0:  worldbody (zero)
# 1:  pelvis

# 2:  left hip roll 
# 3:  left hip yaw
# 4:  left hip pitch
# 5:  left achilles rod
# 6:  left knee
# 7:  left knee spring
# 8:  left shin
# 9:  left tarsus
# 10:  left heel spring
# 12:  left foot crank
# 12: left plantar rod
# 13: left foot

# 14: right hip roll 
# 15: right hip yaw
# 16: right hip pitch
# 17: right achilles rod
# 18: right knee
# 19: right knee spring
# 20: right shin
# 21: right tarsus
# 22: right heel spring
# 23: right foot crank
# 24: right plantar rod
# 25: right foot


# qpos layout
# [ 0] Pelvis x
# [ 1] Pelvis y
# [ 2] Pelvis z
# [ 3] Pelvis orientation qw
# [ 4] Pelvis orientation qx
# [ 5] Pelvis orientation qy
# [ 6] Pelvis orientation qz
# [ 7] Left hip roll         (Motor [0])
# [ 8] Left hip yaw          (Motor [1])
# [ 9] Left hip pitch        (Motor [2])
# [10] Left achilles rod qw
# [11] Left achilles rod qx
# [12] Left achilles rod qy
# [13] Left achilles rod qz
# [14] Left knee             (Motor [3])
# [15] Left shin                        (Joint [0])
# [16] Left tarsus                      (Joint [1])
# [17] Left heel spring
# [18] Left foot crank
# [19] Left plantar rod
# [20] Left foot             (Motor [4], Joint [2])
# [21] Right hip roll        (Motor [5])
# [22] Right hip yaw         (Motor [6])
# [23] Right hip pitch       (Motor [7])
# [24] Right achilles rod qw
# [25] Right achilles rod qx
# [26] Right achilles rod qy
# [27] Right achilles rod qz
# [28] Right knee            (Motor [8])
# [29] Right shin                       (Joint [3])
# [30] Right tarsus                     (Joint [4])
# [31] Right heel spring
# [32] Right foot crank
# [33] Right plantar rod
# [34] Right foot            (Motor [9], Joint [5])

# qvel layout
# [ 0] Pelvis x
# [ 1] Pelvis y
# [ 2] Pelvis z
# [ 3] Pelvis orientation wx
# [ 4] Pelvis orientation wy
# [ 5] Pelvis orientation wz
# [ 6] Left hip roll         (Motor [0])
# [ 7] Left hip yaw          (Motor [1])
# [ 8] Left hip pitch        (Motor [2])
# [ 9] Left knee             (Motor [3])
# [10] Left shin                        (Joint [0])
# [11] Left tarsus                      (Joint [1])
# [12] Left foot             (Motor [4], Joint [2])
# [13] Right hip roll        (Motor [5])
# [14] Right hip yaw         (Motor [6])
# [15] Right hip pitch       (Motor [7])
# [16] Right knee            (Motor [8])
# [17] Right shin                       (Joint [3])
# [18] Right tarsus                     (Joint [4])
# [19] Right foot            (Motor [9], Joint [5])
