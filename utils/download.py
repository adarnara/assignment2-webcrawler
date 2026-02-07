import requests
import cbor
import time

from utils.response import Response


def download(url, config, logger=None):
    host, port = config.cache_server
    try:
        resp = requests.get(
            f"http://{host}:{port}/",
            params=[("q", f"{url}"), ("u", f"{config.user_agent}")],
            timeout=30
        )
        if resp and resp.content:
            return Response(cbor.loads(resp.content))
    except (EOFError, ValueError) as e:
        if logger:
            logger.error(f"Error decoding response for {url}: {e}")
    except requests.exceptions.RequestException as e:
        if logger:
            logger.error(f"Request error for {url}: {e}")
        return Response({
            "error": f"Request error: {e}",
            "status": 0,
            "url": url
        })
    
    if logger:
        logger.error(f"Spacetime Response error {resp} with url {url}.")
    return Response({
        "error": f"Spacetime Response error {resp} with url {url}.",
        "status": resp.status_code if resp else 0,
        "url": url})
