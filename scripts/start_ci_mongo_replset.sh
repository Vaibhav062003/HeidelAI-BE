#!/usr/bin/env bash
set -euo pipefail

container_name="heidelai-mongo-rs"
docker run --detach --rm --name "${container_name}" --publish 27017:27017 \
  mongo:7.0 --replSet rs0 --bind_ip_all

ready="false"
for _attempt in $(seq 1 90); do
  if docker exec "${container_name}" mongosh --quiet --eval 'db.adminCommand({ping: 1}).ok' >/dev/null 2>&1; then
    ready="true"
    break
  fi
  sleep 1
done
if [[ "${ready}" != "true" ]]; then
  docker logs "${container_name}"
  exit 1
fi

docker exec "${container_name}" mongosh --quiet --eval \
  'rs.initiate({_id: "rs0", members: [{_id: 0, host: "localhost:27017"}]})'

primary="false"
for _attempt in $(seq 1 90); do
  if [[ "$(docker exec "${container_name}" mongosh --quiet --eval 'db.hello().isWritablePrimary')" == "true" ]]; then
    primary="true"
    break
  fi
  sleep 1
done
if [[ "${primary}" != "true" ]]; then
  docker logs "${container_name}"
  exit 1
fi
