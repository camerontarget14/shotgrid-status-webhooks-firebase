"""• ShotGrid webhooks – Firebase Cloud Functions
----------------------------------------------------------------
Exports three HTTP Cloud Functions that preserve legacy URLs while sharing a
single implementation.

- task_webhook            – Task status change
- version_webhook         – Version status change
- version_created_webhook – Version created

Deploy (Gen‑2):
    firebase deploy --only functions
"""
from __future__ import annotations
import os, json, hmac, hashlib, yaml, logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
import shotgun_api3
import functions_framework            # local dev convenience
from firebase_functions import https_fn  # GCF/Firebase runtime
from flask import Request, abort, make_response, jsonify

# ─────────────────────────────── Standard Python Logging ────────────────────────
# Set up a logger with a name in Firebase Functions
logger = logging.getLogger("shotgrid-webhooks")

# Configure the logger
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Configure the handler (Firebase Functions automatically captures stdout/stderr)
handler = logging.StreamHandler()
handler.setFormatter(formatter)

# Add the handler to the logger
logger.addHandler(handler)

# Set the logging level
logger.setLevel(logging.INFO)

# ─────────────────────────────── Configuration ──────────────────────────────
ROOT = os.path.dirname(__file__)
with open(os.path.join(ROOT, "config.json"), "rt", encoding="utf8") as f:
    _CONF = json.load(f)
with open(os.path.join(ROOT, "status_mapping.yaml"), "rt", encoding="utf8") as f:
    _MAP = yaml.safe_load(f)

SG_HOST        = _CONF["SHOTGRID_URL"]
SG_API_KEY     = _CONF["SHOTGRID_API_KEY"]
SG_SCRIPT_NAME = _CONF["SHOTGRID_SCRIPT_NAME"]
SECRET_TOKEN   = _CONF["SECRET_TOKEN"].encode()

logger.info("Starting ShotGrid webhooks service")
logger.info(f"Using ShotGrid host: {SG_HOST}")
logger.info(f"Using script name: {SG_SCRIPT_NAME}")

# ─────────────────────────────── Singleton SG client ────────────────────────
logger.info("Initializing ShotGrid client connection")
try:
    _SG_CLIENT = shotgun_api3.Shotgun(
        SG_HOST,
        script_name=SG_SCRIPT_NAME,
        api_key=SG_API_KEY,
        connect=True,
    )
    logger.info("ShotGrid client connection successful")
except Exception as e:
    logger.error(f"Failed to initialize ShotGrid client: {str(e)}")
    raise

# ─────────────────────────────── ShotGrid helper ────────────────────────────
class SG:
    """Lightweight wrapper re‑using one persistent ShotGrid session."""
    def __init__(self):
        self._sg = _SG_CLIENT

    # Queries
    def find_version(self, vid: int):
        logger.info(f"Finding Version {vid}")
        try:
            result = self._sg.find_one(
                "Version", [["id", "is", vid]],
                ["id", "sg_task", "sg_status_list", "entity", "project"],
            )
            if result:
                logger.info(f"Found Version {vid} with status {result.get('sg_status_list')}")
                task_id = (result.get("sg_task") or {}).get("id")
                logger.info(f"Version {vid} is linked to Task {task_id}" if task_id else f"Version {vid} has no linked Task")
            else:
                logger.warning(f"Version {vid} not found")
            return result
        except Exception as e:
            logger.error(f"Error finding Version {vid}: {str(e)}")
            return None

    def find_task(self, tid: int):
        logger.info(f"Finding Task {tid}")
        try:
            result = self._sg.find_one(
                "Task", [["id", "is", tid]],
                ["id", "step", "sg_status_list", "entity", "project"],
            )
            if result:
                step_name = (result.get("step") or {}).get("name")
                logger.info(f"Found Task {tid} with status {result.get('sg_status_list')} and step {step_name}")
            else:
                logger.warning(f"Task {tid} not found")
            return result
        except Exception as e:
            logger.error(f"Error finding Task {tid}: {str(e)}")
            return None

    def find_shot(self, sid: int):
        logger.info(f"Finding Shot {sid}")
        try:
            result = self._sg.find_one(
                "Shot", [["id", "is", sid]], ["id", "sg_status_list", "code"],
            )
            if result:
                logger.info(f"Found Shot {sid} ({result.get('code')}) with status {result.get('sg_status_list')}")
            else:
                logger.warning(f"Shot {sid} not found")
            return result
        except Exception as e:
            logger.error(f"Error finding Shot {sid}: {str(e)}")
            return None

    # Mutations
    def set_task_status(self, ids: List[int], status: str):
        logger.info(f"Setting Task status to {status} for IDs: {ids}")
        try:
            batch = [
                {"request_type": "update", "entity_type": "Task", "entity_id": tid,
                "data": {"sg_status_list": status}} for tid in ids
            ]
            result = self._sg.batch(batch)
            logger.info(f"Task status update successful: {result}")
            return result
        except Exception as e:
            logger.error(f"Error updating Task statuses: {str(e)}")
            return None

    def set_shot_status(self, sid: int, status: str):
        logger.info(f"Setting Shot {sid} status to {status}")
        try:
            result = self._sg.update("Shot", sid, {"sg_status_list": status})
            logger.info(f"Shot {sid} status update successful: {result}")
            return result
        except Exception as e:
            logger.error(f"Error updating Shot {sid} status: {str(e)}")
            return None

    def set_version_status(self, vid: int, status: str):
        logger.info(f"Setting Version {vid} status to {status}")
        try:
            result = self._sg.update("Version", vid, {"sg_status_list": status})
            logger.info(f"Version {vid} status update successful: {result}")
            return result
        except Exception as e:
            logger.error(f"Error updating Version {vid} status: {str(e)}")
            return None

# ─────────────────────────────── YAML mappings ──────────────────────────────
_V2T: Dict[str, List[str]] = _MAP.get("version_to_task", {})
_T2S: Dict[str, List[str]] = _MAP.get("task_to_shot", {})
logger.info(f"Loaded version-to-task mappings: {json.dumps(_V2T)}")
logger.info(f"Loaded task-to-shot mappings: {json.dumps(_T2S)}")

map_version_to_task = lambda v: _V2T.get(v, [])
map_task_to_shot   = lambda t: _T2S.get(t, [])

# ─────────────────────────────── Helper utils ───────────────────────────────

def _verify_sig(body: bytes, sig: Optional[str]) -> bool:
    logger.info("Verifying webhook signature")
    if not sig:
        logger.warning("No signature provided in request")
        return False
    sig = sig[5:] if sig.startswith("sha1=") else sig
    expected = hmac.new(SECRET_TOKEN, body, hashlib.sha1).hexdigest()
    result = hmac.compare_digest(expected, sig)
    if result:
        logger.info("Signature verification successful")
    else:
        logger.warning("Signature verification failed")
    return result


def _entity_id(data: dict) -> Optional[int]:
    logger.info("Extracting entity ID from payload")
    entity_id = None
    if "entity_id" in data:
        entity_id = data["entity_id"]
        logger.info(f"Found entity_id: {entity_id}")
    else:
        ent = data.get("entity")
        if isinstance(ent, dict):
            entity_id = ent.get("id")
            logger.info(f"Found entity.id: {entity_id}")

    if entity_id is None:
        logger.warning("No entity ID found in payload")

    return entity_id


def _is_composite_step(task: dict) -> bool:
    step = (task or {}).get("step") or {}
    step_name = step.get("name")
    result = step_name in {"Composite", "Secondary Composite"}
    logger.info(f"Checking if step '{step_name}' is composite: {result}")
    return result


def _update_linked_shot_if_needed(sg: SG, task: dict, candidate: List[str]):
    logger.info(f"Checking if linked Shot needs status update to one of: {candidate}")

    if not candidate:
        logger.info("No candidate statuses provided for Shot, skipping")
        return None, None

    if "entity" not in task:
        logger.info("Task has no linked entity, skipping")
        return None, None

    shot_id = task["entity"].get("id")
    if not shot_id:
        logger.info("Task's linked entity has no ID, skipping")
        return None, None

    shot = sg.find_shot(shot_id)
    if not shot:
        logger.warning(f"Linked Shot {shot_id} not found")
        return None, None

    current_status = shot["sg_status_list"]
    if current_status in candidate:
        logger.info(f"Shot {shot_id} already has status '{current_status}' which is in candidate list, skipping update")
        return shot, None

    logger.info(f"Updating Shot {shot_id} status from '{current_status}' to '{candidate[0]}'")
    result = sg.set_shot_status(shot_id, candidate[0])
    return shot, result

# ─────────────────────────────── Handlers ───────────────────────────────────

def _handle_version_status(payload: dict):
    logger.info("Version status webhook triggered")
    # Use debug level for large payloads
    logger.debug(f"Version status payload: {json.dumps(payload)}")

    meta = payload["data"].get("meta", {})
    attribute_name = meta.get("attribute_name")

    if attribute_name != "sg_status_list":
        logger.info(f"Ignoring update to attribute '{attribute_name}', only handling sg_status_list")
        return {"ignored": True, "reason": f"attribute_name is '{attribute_name}', not 'sg_status_list'"}

    vid = _entity_id(payload["data"])
    if vid is None:
        logger.error("Failed to extract entity ID from payload")
        return {"error": "No entity id"}

    new_status = meta.get("new_value")
    old_status = meta.get("old_value")
    logger.info(f"Version {vid} status changed from '{old_status}' to '{new_status}'")

    sg = SG()
    version = sg.find_version(vid) or {}

    task_id = (version.get("sg_task") or {}).get("id")
    logger.info(f"Version {vid} is linked to Task {task_id}" if task_id else f"Version {vid} has no linked Task")

    task = sg.find_task(task_id) if task_id else None

    task_statuses = map_version_to_task(new_status)
    logger.info(f"Mapped Version status '{new_status}' to Task statuses: {task_statuses}")

    if task and task_statuses and task["sg_status_list"] not in task_statuses:
        current_task_status = task["sg_status_list"]
        target_task_status = task_statuses[0]
        logger.info(f"Updating Task {task_id} status from '{current_task_status}' to '{target_task_status}'")

        sg.set_task_status([task_id], target_task_status)

        shot_statuses = map_task_to_shot(target_task_status)
        logger.info(f"Mapped Task status '{target_task_status}' to Shot statuses: {shot_statuses}")

        shot_before, shot_update = _update_linked_shot_if_needed(sg, task, shot_statuses)
        if shot_update:
            logger.info(f"Updated linked Shot from '{shot_before['sg_status_list']}' to '{shot_update['sg_status_list']}'")
    else:
        if not task:
            logger.info(f"No Task found for Task ID {task_id}, skipping Task update")
        elif not task_statuses:
            logger.info(f"No mapped Task statuses for Version status '{new_status}', skipping Task update")
        else:
            logger.info(f"Task {task_id} already has status '{task['sg_status_list']}' which matches mapping, skipping update")

    return {"version_id": vid, "task_id": task_id, "new_status": new_status}


def _handle_task_status(payload: dict):
    logger.info("Task status webhook triggered")
    logger.debug(f"Task status payload: {json.dumps(payload)}")

    meta = payload["data"].get("meta", {})
    attribute_name = meta.get("attribute_name")

    if attribute_name != "sg_status_list":
        logger.info(f"Ignoring update to attribute '{attribute_name}', only handling sg_status_list")
        return {"ignored": True, "reason": f"attribute_name is '{attribute_name}', not 'sg_status_list'"}

    tid = _entity_id(payload["data"])
    if tid is None:
        logger.error("Failed to extract entity ID from payload")
        return {"error": "No entity id"}

    new_status = meta.get("new_value")
    old_status = meta.get("old_value")
    logger.info(f"Task {tid} status changed from '{old_status}' to '{new_status}'")

    sg = SG()
    task = sg.find_task(tid)

    if not task:
        logger.error(f"Task {tid} not found")
        return {"error": f"Task {tid} not found"}

    if not _is_composite_step(task):
        logger.info(f"Task {tid} is not in a Composite step, ignoring")
        return {"ignored": True, "reason": "Not a composite step task"}

    shot_statuses = map_task_to_shot(new_status)
    logger.info(f"Mapped Task status '{new_status}' to Shot statuses: {shot_statuses}")

    shot_before, shot_update = _update_linked_shot_if_needed(sg, task, shot_statuses)

    if shot_update:
        before_status = shot_before["sg_status_list"] if shot_before else None
        after_status = shot_update["sg_status_list"] if shot_update else None
        logger.info(f"Updated linked Shot from '{before_status}' to '{after_status}'")
    else:
        logger.info("No Shot update performed")

    return {
        "task_id": tid,
        "new_status": new_status,
        "shot_before": shot_before["sg_status_list"] if shot_before else None,
        "shot_after": shot_update["sg_status_list"] if shot_update else None
    }


def _handle_version_created(payload: dict):
    """Set new Versions to status `cnv` only for Prep, Composite, or Computer Graphics steps."""
    logger.info("Version created webhook triggered")
    logger.debug(f"Version created payload: {json.dumps(payload)}")

    vid = _entity_id(payload["data"])
    if vid is None:
        logger.error("Failed to extract entity ID from payload")
        return {"error": "No entity id"}

    sg = SG()
    version = sg.find_version(vid)

    if not version:
        logger.error(f"Version {vid} not found")
        return {"error": f"Version {vid} not found"}

    status_before = version["sg_status_list"]
    logger.info(f"New Version {vid} initial status: '{status_before}'")

    step_name = None
    task = None
    task_id = None

    if version.get("sg_task"):
        task_id = version["sg_task"]["id"]
        logger.info(f"Version {vid} is linked to Task {task_id}")

        task = sg.find_task(task_id)
        if task:
            step_name = (task.get("step") or {}).get("name")
            logger.info(f"Task {task_id} is in step '{step_name}' with status '{task.get('sg_status_list')}'")
        else:
            logger.warning(f"Failed to fetch Task {task_id} details")
    else:
        logger.info(f"Version {vid} has no linked Task")

    # Only specific pipeline steps should get 'cnv' status
    eligible_steps = ["Prep", "Composite", "Computer Graphics"]

    if step_name in eligible_steps:
        logger.info(f"Setting Version {vid} status from '{status_before}' to 'cnv' (in eligible step: {step_name})")
        sg.set_version_status(vid, "cnv")
        status_after = "cnv"

        # Propagate the status to tasks
        task_statuses = map_version_to_task("cnv")
        logger.info(f"Mapped Version status 'cnv' to Task statuses: {task_statuses}")

        if task and task_statuses and task["sg_status_list"] not in task_statuses:
            current_task_status = task["sg_status_list"]
            target_task_status = task_statuses[0]
            logger.info(f"Updating Task {task_id} status from '{current_task_status}' to '{target_task_status}'")

            sg.set_task_status([task_id], target_task_status)

            shot_statuses = map_task_to_shot(target_task_status)
            logger.info(f"Mapped Task status '{target_task_status}' to Shot statuses: {shot_statuses}")

            shot_before, shot_update = _update_linked_shot_if_needed(sg, task, shot_statuses)
            if shot_update:
                before_status = shot_before["sg_status_list"] if shot_before else None
                after_status = shot_update["sg_status_list"] if shot_update else None
                logger.info(f"Updated linked Shot from '{before_status}' to '{after_status}'")
        else:
            if not task:
                logger.info(f"No Task found for Task ID {task_id}, skipping Task update")
            elif not task_statuses:
                logger.info(f"No mapped Task statuses for Version status 'cnv', skipping Task update")
            else:
                logger.info(f"Task {task_id} already has status '{task['sg_status_list']}' which matches mapping, skipping update")
    else:
        # Set to 'na' if not in eligible steps and not already 'na'
        if step_name not in eligible_steps and status_before != "na":
            logger.info(f"Setting Version {vid} status from '{status_before}' to 'na' (not in eligible step)")
            sg.set_version_status(vid, "na")
            status_after = "na"
        else:
            status_after = status_before
            if step_name in eligible_steps:
                logger.info(f"Version {vid} already has status 'cnv', no update needed")
            else:
                logger.info(f"Version {vid} already has status 'na', no update needed")

    return {
        "version_id": vid,
        "pipeline_step": step_name,
        "status_before": status_before,
        "status_after": status_after,
        "task_id": task_id
    }

# ─────────────────────────────── Dispatcher ────────────────────────────────

def _dispatch(request: Request, route: Optional[str] = None):
    path = request.path
    key = (route or path.rstrip("/").split("/")[-1]).lower()
    logger.info(f"Received webhook request to path '{path}', dispatching as '{key}'")

    body_data = request.get_data()
    logger.debug(f"Request body size: {len(body_data)} bytes")

    sig = request.headers.get("X-SG-Signature")
    if not _verify_sig(body_data, sig):
        logger.warning(f"Unauthorized request to {path}: Invalid signature")
        abort(make_response(("Unauthorized", 401)))

    try:
        payload = request.get_json(force=True)
        logger.debug(f"Parsed JSON payload type: {payload.get('event_type', 'unknown')}")
    except Exception as e:
        logger.error(f"Failed to parse JSON from request: {str(e)}")
        abort(make_response(("Bad JSON", 400)))

    if key == "task":
        logger.info("Handling as task webhook")
        result = _handle_task_status(payload)
    elif key in {"version", "status"}:
        logger.info("Handling as version status webhook")
        result = _handle_version_status(payload)
    elif key in {"version_created", "version-created"}:
        logger.info("Handling as version created webhook")
        result = _handle_version_created(payload)
    else:
        logger.warning(f"Unknown webhook type: {key}")
        abort(make_response(("Not Found", 404)))

    ts = payload.get("timestamp")
    if ts:
        try:
            ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            lag_ms = int((datetime.now(timezone.utc) - ts_dt).total_seconds()*1000)
            result["lag_ms"] = lag_ms
            logger.info(f"Event processing lag: {lag_ms}ms")
        except Exception as e:
            logger.warning(f"Bad timestamp '{ts}': {str(e)}")

    logger.info(f"Webhook {key} processing complete: {json.dumps(result)}")
    return jsonify(result), 200

# ─────────────────────────────── Cloud Function exports ────────────────────
@https_fn.on_request()
def task_webhook(request: Request):
    logger.info("task_webhook function called")
    return _dispatch(request, "task")

@https_fn.on_request()
def version_webhook(request: Request):
    logger.info("version_webhook function called")
    return _dispatch(request, "version")

@https_fn.on_request()
def version_created_webhook(request: Request):
    logger.info("version_created_webhook function called")
    return _dispatch(request, "version_created")

# Local testing entrypoint
@functions_framework.http
def main(request: Request):
    logger.info("main function called (local development)")
    return _dispatch(request)
