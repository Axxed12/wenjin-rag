#!/bin/sh
set -eu

load_secret() {
    name="$1"
    path="$2"
    if [ -r "$path" ]; then
        value=$(cat "$path")
        if [ -z "$value" ]; then
            echo "required secret file is empty" >&2
            exit 78
        fi
        export "$name=$value"
    fi
}

# Docker secret files are read into the process environment without printing them.
load_secret WENJIN_MODEL_API_TOKEN /run/secrets/model_api_token
load_secret WENJIN_FILE_ADMIN_TOKEN /run/secrets/file_admin_token
load_secret QDRANT_API_KEY /run/secrets/qdrant_api_key

if [ "${WENJIN_MODEL_PROVIDER:-local}" = "remote" ] && [ -z "${WENJIN_MODEL_API_TOKEN:-}" ]; then
    echo "remote model provider requires the model API token secret" >&2
    exit 78
fi

exec "$@"
