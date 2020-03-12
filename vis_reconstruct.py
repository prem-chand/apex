import torch
import numpy as np
import tty
import termios
import select
import pickle
import sys
import time

from cassie.quaternion_function import *
from cassie import CassieEnv, CassieEnv_latent, CassieStandingEnv
from cassie.cassiemujoco.cassiemujoco import CassieSim, CassieVis
from cassie.vae import VAE
from torch import nn

def isData():
    return select.select([sys.stdin], [], [], 0) == ([sys.stdin], [], [])

def vis_policy(latent_model, norm_params):
    # Load policy and env args
    eval_path = "./trained_models/latent_space/Cassie-v0/5b75b3-seed0/"
    run_args = pickle.load(open(eval_path + "experiment.pkl", "rb"))
    policy = torch.load(eval_path + "actor.pt")

    # Load latent model
    latent_size = 35
    hidden_size = 40
    # latent_model = VAE(hidden_size, latent_size, mj_state=True)
    # saved_state_dict = torch.load("./vae_model/mj_state_test_latent{}_hidden{}.pt".format(latent_size, hidden_size), map_location=torch.device('cpu'))
    # latent_model.load_state_dict(saved_state_dict)
    # print(latent_model)
    # exit()

    # Make interaction env and reconstruction sim/vis
    env = CassieEnv(traj=run_args.traj, state_est=run_args.state_est, dynamics_randomization=run_args.dyn_random, clock_based=run_args.clock_based, history=run_args.history)
    print("obs dim: ", env._obs)
    # NOTE: BUG!!!!! For some reason get consistent seg faults if use env.vis rather than a separate CassieVis object. Don't know why
    # For now, seems like if want to have multiple CassieVis objects need to have the actual CassieVis objects separately instead of using/creating
    # them in a CassieEnv.......... wtf
    policy_vis = CassieVis(env.sim, "./cassie/cassiemujoco/cassie.xml")
    reconstruct_sim = CassieSim("./cassie/cassiemujoco/cassie.xml")
    reconstruct_vis = CassieVis(reconstruct_sim, "./cassie/cassiemujoco/cassie.xml")
    # print("Made both env and vis")
    # print("env sim id: ", id(env.sim))
    # print("reconstruct sim id: ", id(reconstruct_sim))
    # print("reconstruct vis id: ", id(reconstruct_vis))
    # norm_params = np.load("./data_norm_params.npz")
    data_max = norm_params["data_max"]
    data_min = norm_params["data_min"]
    print("data max shape: ", data_max.shape)


    old_settings = termios.tcgetattr(sys.stdin)

    orient_add = 0
    perturb_duration = 0.2
    perturb_start = -100
    force_arr = np.zeros(6)
    timesteps = 0
    reconstruct_err = np.zeros(35)

    # Inital render of both vis's
    # env_render_state = env.render()
    policy_render_state = policy_vis.draw(env.sim)
    reconstruct_render_state = reconstruct_vis.draw(reconstruct_sim)
    try:
        tty.setcbreak(sys.stdin.fileno())

        state = env.reset_for_test()
        done = False
        speed = 0.0

        while policy_render_state and reconstruct_render_state:
        
            if isData():
                c = sys.stdin.read(1)
                if c == 'w':
                    speed += 0.1
                    env.update_speed(speed)
                    print("speed: ", env.speed)
                elif c == 's':
                    speed -= 0.1
                    env.update_speed(speed)
                    print("speed: ", env.speed)
                elif c == 'l':
                    orient_add += .1
                    print("Increasing orient_add to: ", orient_add)
                elif c == 'k':
                    orient_add -= .1
                    print("Decreasing orient_add to: ", orient_add)
                elif c == 'p':
                    print("set perturb time")
                    push = -50
                    push_dir = 1
                    force_arr = np.zeros(6)
                    force_arr[push_dir] = push
                    # env.sim.apply_force(force_arr)
                    perturb_start = env.sim.time()
            
            # If model is reset (pressing backspace while in vis window) then need to reset
            # perturb_start as well
            if env.sim.time() == 0:
                perturb_start = -100

            if (not policy_vis.ispaused()) and (not reconstruct_vis.ispaused()):
                # Update Orientation
                quaternion = euler2quat(z=orient_add, y=0, x=0)
                iquaternion = inverse_quaternion(quaternion)

                if env.state_est:
                    curr_orient = state[1:5]
                    curr_transvel = state[14:17]
                else:
                    curr_orient = state[2:6]
                    curr_transvel = state[20:23]
                
                new_orient = quaternion_product(iquaternion, curr_orient)

                if new_orient[0] < 0:
                    new_orient = -new_orient

                new_translationalVelocity = rotate_by_quaternion(curr_transvel, iquaternion)
                
                if env.state_est:
                    state[1:5] = torch.FloatTensor(new_orient)
                    state[14:17] = torch.FloatTensor(new_translationalVelocity)
                    # state[0] = 1      # For use with StateEst. Replicate hack that height is always set to one on hardware.
                else:
                    state[2:6] = torch.FloatTensor(new_orient)
                    state[20:23] = torch.FloatTensor(new_translationalVelocity)
                    
                # Apply perturb if needed
                if env.sim.time() - perturb_start < perturb_duration:
                    policy_vis.apply_force(force_arr, "cassie-pelvis")

                with torch.no_grad():
                    action = policy.forward(torch.Tensor(state), deterministic=True).detach().numpy()
                state, reward, done, _ = env.step(action)
                
                # Update reconstruct state
                # mj_state = env.sim.qpos()#np.concatenate([env.sim.qpos(), env.sim.qvel()])
                norm_state = np.divide((np.array(env.sim.qpos()) - data_min), data_max)
                norm_state = np.random.rand(35)
                decode_state, mu, logvar = latent_model.forward(torch.Tensor(norm_state))
                decode_state = decode_state.detach().numpy()[0]
                reconstruct_state = (decode_state*data_max) + data_min
                # reconstruct_state += data_min
                print("reconstruct state: ", reconstruct_state)
                # print("mj state: ", mj_state)
                # print("mu: ", mu)
                # reconstruct_state[3:7] = mj_state[3:7]
                reconstruct_sim.set_qpos(reconstruct_state[0:35])
                # reconstruct_sim.set_qvel(reconstruct_state[35:35+32])
                reconstruct_err += norm_state - decode_state

                timesteps += 1

            # Render both env and recosntruct_vis
            # render_state = env.render()
            policy_render_state = policy_vis.draw(env.sim)
            reconstruct_vis.draw(reconstruct_sim)
            time.sleep(0.02)


    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)

    print("Average reconstruction error: ", np.linalg.norm(reconstruct_err) / timesteps)

# Reconstruction + KL divergence losses summed over all elements and batch
def loss_function(recon_x, x, mu, logvar):
    print("recon shape:", recon_x.shape)
    print("x shape: ", x.flatten().shape)
    recon_loss_cri = nn.MSELoss()

    MSE = 35 * recon_loss_cri(recon_x, x.view(-1, 35))

    # see Appendix B from VAE paper:
    # Kingma and Welling. Auto-Encoding Variational Bayes. ICLR, 2014
    # https://arxiv.org/abs/1312.6114
    # 0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
    KLD = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())

    return MSE + KLD

def eval_recon(recon_data, data):
    

def vis_traj(latent_model, norm_params):
    data = np.load("./5b75b3-seed0_full_mjdata.npz")
    state_data = data["total_data"]
    state_data = state_data[:, 0:-32]
    data_len =  state_data.shape[0]
    # norm_params = np.load("./data_norm_params_qpos_entropyloss.npz")
    data_max = norm_params["data_max"]
    data_min = norm_params["data_min"]
    norm_data = np.divide((state_data-data_min), data_max)
    norm_data = torch.Tensor(norm_data)


    # Load latent model
    latent_size = 35
    hidden_size = 40
    # latent_model = VAE(hidden_size, latent_size, mj_state=True)
    # saved_state_dict = torch.load("./vae_model/mj_state_qpos_entropyloss_latent{}_hidden{}.pt".format(latent_size, hidden_size), map_location=torch.device('cpu'))
    # latent_model.load_state_dict(saved_state_dict)

    decode, mu, log_var = latent_model.forward(norm_data)
    print("log_var shape: ", torch.mean(log_var, axis=0))
    loss = loss_function(decode, norm_data, mu, log_var)
    print("loss: ", loss/data_len)

# Load latent model
latent_size = 35
hidden_size = 40
latent_model = VAE(hidden_size, latent_size, mj_state=True)
saved_state_dict = torch.load("./vae_model/mj_state_qpos_entropyloss_latent{}_hidden{}.pt".format(latent_size, hidden_size), map_location=torch.device('cpu'))
latent_model.load_state_dict(saved_state_dict)
norm_params = np.load("./data_norm_params_qpos_entropyloss.npz")


vis_traj(latent_model, norm_params)
# vis_policy(latent_model, norm_params)