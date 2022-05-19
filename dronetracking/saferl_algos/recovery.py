import copy
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from saferl_utils import C_Critic,Critic,Actor

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class TD3Recovery(object):
    def __init__(
        self,
        state_dim,
        action_dim,
        max_action,
        delta,
        rew_discount=0.99,
        cost_discount=0.99,
        tau=0.005,
        policy_noise=0.2,
        noise_clip=0.5,
        policy_freq=2,
        expl_noise = 0.1,
    ):  
        
        self.action_dim = action_dim
        self.max_action = max_action
        self.delta = delta
        self.rew_discount = rew_discount
        self.cost_discount = cost_discount
        self.tau = tau
        self.policy_noise = policy_noise
        self.noise_clip = noise_clip
        self.policy_freq = policy_freq

        self.expl_noise = 0.1

        self.total_it = 0

        # Pi_task (task policy)
        self.actor = Actor(state_dim, action_dim, max_action).to(device)
        self.actor_target = copy.deepcopy(self.actor)
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=3e-4)

        self.critic = Critic(state_dim, action_dim).to(device)
        self.critic_target = copy.deepcopy(self.critic)
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=3e-4)

        # Pi_recovery (recovery policy)
        self.C_actor = Actor(state_dim, action_dim, max_action).to(device)
        self.C_actor_target = copy.deepcopy(self.C_actor)
        self.C_actor_optimizer = torch.optim.Adam(self.C_actor.parameters(), lr=3e-4)

        self.C_critic = C_Critic(state_dim, action_dim).to(device)
        self.C_critic_target = copy.deepcopy(self.C_critic)
        self.C_critic_optimizer = torch.optim.Adam(self.C_critic.parameters(), lr=3e-4)


    def select_action(self, state, recovery=False, exploration=False):
        state = torch.FloatTensor(state.reshape(1, -1)).to(device)
        if recovery and self.C_critic_target(state,self.actor(state)).item() > self.delta:
            action = self.C_actor(state).cpu().data.numpy().flatten()
            raw_action = self.actor(state).cpu().data.numpy().flatten()
        else:
            action = self.actor(state).cpu().data.numpy().flatten()
            raw_action = action
        if exploration:
            noise = np.random.normal(0, self.max_action * self.expl_noise, size=self.action_dim)
            action = (action + noise).clip(-self.max_action, self.max_action)
        return action,raw_action

    def pred_cost(self, state, action):
        state = torch.FloatTensor(state.reshape(1, -1)).to(device)
        action = torch.FloatTensor(action.reshape(1, -1)).to(device)
        return self.C_critic(state,action).item()
    
    def train(self, replay_buffer, batch_size=256):
        # Sample replay buffer 
        state, action,rec_action, next_state, reward, cost, not_done = replay_buffer.sample(batch_size)
        #------------------------------------------------------------------------------------------------
        # Compute the target C value
        target_C = self.C_critic_target(next_state, self.C_actor_target(next_state))
        target_C = cost + (1.-cost) * (not_done * self.cost_discount * target_C).detach()

        # Get current C estimate
        current_C = self.C_critic(state, rec_action)

        # Compute critic loss
        C_critic_loss = F.mse_loss(current_C, target_C)

        # Optimize the critic
        self.C_critic_optimizer.zero_grad()
        C_critic_loss.backward()
        self.C_critic_optimizer.step()
        #------------------------------------------------------------------------------------------------
        with torch.no_grad():
            # Select action according to policy and add clipped noise
            noise = (
                torch.randn_like(action) * self.policy_noise
            ).clamp(-self.noise_clip, self.noise_clip)
            
            next_action = (
                self.actor_target(next_state) + noise
            ).clamp(-self.max_action, self.max_action)

            # Compute the target Q value
            target_Q1, target_Q2 = self.critic_target(next_state, next_action)
            target_Q = torch.min(target_Q1, target_Q2)
            target_Q = reward + not_done * self.rew_discount * target_Q

        # Get current Q estimates
        current_Q1, current_Q2 = self.critic(state, action)

        # Compute critic loss
        critic_loss = F.mse_loss(current_Q1, target_Q) + F.mse_loss(current_Q2, target_Q)
        
        # Optimize the critic
        self.critic_optimizer.zero_grad()
        critic_loss.backward()
        self.critic_optimizer.step()
        #------------------------------------------------------------------------------------------------

        # Delayed policy updates
        if self.total_it % self.policy_freq == 0:
            #--------------------------------------------------------------------------------------------
            # Compute actor loss
            action = self.actor(state)
            actor_loss = ( -self.critic.Q1(state, action)).mean()
            
            # Optimize the actor 
            self.actor_optimizer.zero_grad()
            actor_loss.backward()
            self.actor_optimizer.step()

            #--------------------------------------------------------------------------------------------
            
            C_action = self.C_actor(state)
            #C_actor_loss =  F.relu(self.C_critic(state, C_action) - self.delta).mean()
            C_actor_loss =  self.C_critic(state, C_action).mean()
            # Optimize the C_actor 
            self.C_actor_optimizer.zero_grad()
            C_actor_loss.backward()
            self.C_actor_optimizer.step()

            #--------------------------------------------------------------------------------------------

            # Update the frozen target models
            for param, target_param in zip(self.critic.parameters(), self.critic_target.parameters()):
                target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)

            for param, target_param in zip(self.actor.parameters(), self.actor_target.parameters()):
                target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)
        
            for param, target_param in zip(self.C_critic.parameters(), self.C_critic_target.parameters()):
                target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)
            
            for param, target_param in zip(self.C_actor.parameters(), self.C_actor_target.parameters()):
                target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)

        
        
    def save(self, filename):
        torch.save(self.critic.state_dict(), filename + "_critic")
        torch.save(self.critic_optimizer.state_dict(), filename + "_critic_optimizer")

        torch.save(self.C_critic.state_dict(), filename + "_C_critic")
        torch.save(self.C_critic_optimizer.state_dict(), filename + "_C_critic_optimizer")
        
        torch.save(self.actor.state_dict(), filename + "_actor")
        torch.save(self.actor_optimizer.state_dict(), filename + "_actor_optimizer")

        torch.save(self.C_actor.state_dict(), filename + "_C_actor")
        torch.save(self.C_actor_optimizer.state_dict(), filename + "_C_actor_optimizer")


    def load(self, filename):
        self.critic.load_state_dict(torch.load(filename + "_critic"))
        self.critic_optimizer.load_state_dict(torch.load(filename + "_critic_optimizer"))
        self.critic_target = copy.deepcopy(self.critic)

        self.C_critic.load_state_dict(torch.load(filename + "_C_critic"))
        self.C_critic_optimizer.load_state_dict(torch.load(filename + "_C_critic_optimizer"))
        self.C_critic_target = copy.deepcopy(self.C_critic)

        self.actor.load_state_dict(torch.load(filename + "_actor"))
        self.actor_optimizer.load_state_dict(torch.load(filename + "_actor_optimizer"))
        self.actor_target = copy.deepcopy(self.actor)

        self.C_actor.load_state_dict(torch.load(filename + "_C_actor"))
        self.C_actor_optimizer.load_state_dict(torch.load(filename + "_C_actor_optimizer"))
        self.C_actor_target = copy.deepcopy(self.C_actor)


# Runs policy for X episodes and returns average reward
# A fixed seed is used for the eval environment
def eval_policy(policy, eval_env, seed, eval_episodes=5,use_recovery=False):
    avg_reward = 0.
    avg_cost = 0.
    for _ in range(eval_episodes):
        reset_info, done = eval_env.reset(), False
        state = reset_info[0]
        while not done:
            action,raw_action = policy.select_action(np.array(state),recovery=use_recovery,exploration=False)
            state, reward, done, info = eval_env.step(action)
            avg_reward += reward
            if info['constraint_violation']!=0:
                avg_cost += 1

    avg_reward /= eval_episodes
    avg_cost /= eval_episodes

    print("---------------------------------------")
    print(f"Evaluation over {eval_episodes} episodes: {avg_reward:.3f} Cost {avg_cost:.3f}.\
            Use Recovery RL : {use_recovery}")
    print("---------------------------------------")
    return avg_reward,avg_cost