-- Separate databases so evals (frozen snapshot) and tests never touch live data.
CREATE DATABASE career_agent_eval;
CREATE DATABASE career_agent_test;
