#!/usr/bin/env bash

if [ -z $2 ]; then
    echo "Usage: $0 <env_file> <path_to_data>"
    echo "The env file will have your keys and secrets."
    echo "the leaderboard gets stored in the data dir."
    exit 1
fi

environment=$1
data=$2

docker run -d --restart=unless-stopped -v $data:/app/data --env-file $environment --name bot_ross bot_ross
