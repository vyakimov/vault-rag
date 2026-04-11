docker run \
  -it \
  --rm \
  --net=host \
  -v "$PWD:/app/" \
  --env-file .secrets \
  vault-rag /bin/zsh
