#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo '用法：scripts/stack.sh <build|infra|init|up|stop-app|stop|down|ps|logs [服务]>' >&2
}

if (( $# < 1 )); then
  usage
  exit 2
fi

operation=$1
shift
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

env_file="$root/.env"
project=probatio
image_prefix=probatio

case "$operation" in
  build|infra|init|up|stop-app|stop|down|ps)
    if (( $# != 0 )); then usage; exit 2; fi
    ;;
  logs)
    if (( $# > 1 )); then usage; exit 2; fi
    if (( $# == 1 )); then
      case "$1" in
        web|api|maintenance|ingestion-worker|qa-worker|wiki-worker|postgres|redis|elasticsearch|scanner|prepare-storage|initialize) ;;
        *) echo '未知日志服务。' >&2; exit 2 ;;
      esac
    fi
    ;;
  *) usage; exit 2 ;;
esac

if [[ ! -f "$env_file" ]]; then
  printf '缺少配置文件：%s\n' "$env_file" >&2
  exit 1
fi

compose_args=(--project-directory "$root" --project-name "$project"
  --env-file "$env_file" -f "$root/compose.yaml")
compose() {
  # The private configuration file supplies deployment settings.
  env -u APP_SETTINGS -u RELEASE_TAG \
    -u POSTGRES_USER -u POSTGRES_PASSWORD -u POSTGRES_DB \
    -u DOCLING_MODELS_DIR -u BGE_M3_TOKENIZER_DIR \
    -u QA_TOKENIZER_DIR -u CHUNKING_MODEL_TOKENIZER_DIR -u RERANKER_TOKENIZER_DIR \
    -u WEB_BIND_HOST -u WEB_PORT \
    -u CLAMD_CONFIG_FILE \
    -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME -u COMPOSE_PROFILES \
    -u COMPOSE_ENV_FILES -u COMPOSE_DISABLE_ENV_FILE \
    IRA_IMAGE_PREFIX="$image_prefix" \
    docker compose "${compose_args[@]}" "$@"
}

infrastructure=(postgres redis elasticsearch scanner)
backends=(api maintenance ingestion-worker qa-worker wiki-worker)
applications=(web "${backends[@]}")
up_args=(up -d --no-build --pull never --wait --wait-timeout 600)

stop_app() {
  compose stop web api
  compose stop maintenance
  compose stop ingestion-worker qa-worker wiki-worker
}

compose config --quiet
printf '项目：%s；操作：%s\n' "$project" "$operation"
case "$operation" in
  build)
    compose pull --policy missing postgres redis
    compose build api web elasticsearch scanner
    ;;
  infra)
    compose "${up_args[@]}" "${infrastructure[@]}"
    ;;
  init)
    active_services=$(compose ps --all --status running --status restarting --status paused --services "${applications[@]}")
    if [[ -n "$active_services" ]]; then
      printf '初始化前请先执行 scripts/stack.sh stop-app；仍在运行的服务：\n%s\n' "$active_services" >&2
      exit 1
    fi
    compose "${up_args[@]}" "${infrastructure[@]}"
    compose run --rm --no-deps --pull never initialize
    echo '初始化成功；应用尚未启动。'
    ;;
  up)
    compose "${up_args[@]}" "${infrastructure[@]}" "${backends[@]}"
    # Nginx resolves api at startup, so refresh it after the API container is ready.
    compose "${up_args[@]}" --no-deps --force-recreate web
    compose ps -a
    web_binding=$(compose port web 80)
    printf 'Web 宿主监听：%s\n' "$web_binding"
    ;;
  stop-app) stop_app ;;
  stop)
    stop_app
    compose stop "${infrastructure[@]}"
    ;;
  down)
    stop_app
    compose down
    ;;
  ps) compose ps -a ;;
  logs) compose logs --follow --tail 100 "$@" ;;
esac
