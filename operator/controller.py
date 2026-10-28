"""TaskBackup Operator (Unit II, section 2.4).

Reconciles TaskBackup custom resources into pg_dump Jobs:

  TaskBackup (desired state)  ->  PVC "task-backups" (ensured, shared, retained)
                              ->  Job <name>-dump   (owned by the TaskBackup)
  Job status (observed state) ->  TaskBackup.status.phase

kopf supplies the informer/watch, the per-object work queue and the retry
logic; this file only holds the reconciliation logic itself.
"""
import datetime

import kopf
import kubernetes
from kubernetes.client.rest import ApiException

GROUP, VERSION, PLURAL = "dso202.cst.edu.bt", "v1alpha1", "taskbackups"
BACKUP_IMAGE = "sarojsanyasi/dso202-db:1.0"  # same image as the database, so pg_dump matches the server version
RUNNER_SA = "taskbackup-runner"  # defined in rbac.yaml
ACTIVE_PHASES = ("Pending", "Running")


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    # Keep kopf's bookkeeping in annotations with our own prefix, so it never
    # writes unknown fields into .status (the CRD schema would prune them).
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(prefix=GROUP)
    settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(prefix=GROUP)
    settings.persistence.finalizer = f"{GROUP}/finalizer"
    kubernetes.config.load_incluster_config()


def ensure_pvc(namespace, name, size, logger):
    core = kubernetes.client.CoreV1Api()
    try:
        core.read_namespaced_persistent_volume_claim(name, namespace)
        return
    except ApiException as e:
        if e.status != 404:
            raise
    # Deliberately NOT owned by any TaskBackup: backups must outlive the
    # object that requested them.
    core.create_namespaced_persistent_volume_claim(namespace, {
        "metadata": {"name": name, "labels": {"app": "task-tracker", "component": "backup"}},
        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}},
    })
    logger.info(f"Created backup PVC {name} ({size})")


def build_job(name, spec, backup_file):
    db, storage = spec["database"], spec["storage"]
    secret = db["credentialsSecret"]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": f"{name}-dump",
                     "labels": {"app": "task-tracker", "component": "backup", f"{GROUP}/taskbackup": name}},
        "spec": {
            "backoffLimit": 2,
            "activeDeadlineSeconds": 600,
            "template": {
                "metadata": {"labels": {"app": "task-tracker", "component": "backup", f"{GROUP}/taskbackup": name}},
                "spec": {
                    "restartPolicy": "Never",
                    # Mesh identity only (no API access): the db AuthorizationPolicy
                    # admits exactly backend-sa and this account on port 5432.
                    "serviceAccountName": RUNNER_SA,
                    "automountServiceAccountToken": False,
                    "containers": [{
                        "name": "pg-dump",
                        "image": BACKUP_IMAGE,
                        "imagePullPolicy": "IfNotPresent",
                        # Wait for the database (and this Pod's mesh sidecar) before dumping,
                        # so a slow start is not counted as a failed backup attempt.
                        "command": ["sh", "-ec",
                                    'for i in $(seq 30); do pg_isready -h "$DB_HOST" -p "$DB_PORT" -d "$DB_NAME" && break; sleep 2; done\n'
                                    'pg_dump -h "$DB_HOST" -p "$DB_PORT" -d "$DB_NAME" --no-owner -f "/backups/$BACKUP_FILE"\n'
                                    'echo "wrote /backups/$BACKUP_FILE ($(wc -c < "/backups/$BACKUP_FILE") bytes)"\n'
                                    'ls -l /backups'],
                        "env": [
                            {"name": "DB_HOST", "value": db["host"]},
                            {"name": "DB_PORT", "value": str(db["port"])},
                            {"name": "DB_NAME", "value": db["name"]},
                            {"name": "BACKUP_FILE", "value": backup_file},
                            {"name": "PGUSER", "valueFrom": {"secretKeyRef": {"name": secret, "key": "DB_USER"}}},
                            {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {"name": secret, "key": "DB_PASSWORD"}}},
                        ],
                        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                                      "limits": {"cpu": "250m", "memory": "256Mi"}},
                        "volumeMounts": [{"name": "backups", "mountPath": "/backups"}],
                    }],
                    "volumes": [{"name": "backups", "persistentVolumeClaim": {"claimName": storage["pvcName"]}}],
                },
            },
        },
    }


@kopf.on.create(GROUP, VERSION, PLURAL)
def create_backup(spec, name, namespace, body, patch, logger, **_):
    ensure_pvc(namespace, spec["storage"]["pvcName"], spec["storage"]["size"], logger)

    backup_file = f"{name}-{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%SZ}.sql"
    job = build_job(name, spec, backup_file)
    kopf.adopt(job, owner=body)  # ownerReference: deleting the TaskBackup garbage-collects the Job

    try:
        kubernetes.client.BatchV1Api().create_namespaced_job(namespace, job)
    except ApiException as e:
        if e.status != 409:  # 409 = already exists: a retried handler must be idempotent
            raise kopf.TemporaryError(f"Job creation failed: {e.reason}", delay=10)

    patch.status["phase"] = "Running"
    patch.status["jobName"] = job["metadata"]["name"]
    patch.status["backupFile"] = backup_file
    patch.status["startedAt"] = now()
    kopf.info(body, reason="JobCreated", message=f"Started {job['metadata']['name']}")


@kopf.timer(GROUP, VERSION, PLURAL, interval=5, initial_delay=5,
            when=lambda status, **_: status.get("phase") in ACTIVE_PHASES)
def track_job(status, namespace, body, patch, logger, **_):
    """Observe the Job and copy its outcome onto the TaskBackup's status."""
    try:
        job = kubernetes.client.BatchV1Api().read_namespaced_job(status["jobName"], namespace)
    except ApiException as e:
        if e.status == 404:
            patch.status.update(phase="Failed", message="Backup Job disappeared", completedAt=now())
        return

    for cond in job.status.conditions or []:
        if cond.status != "True":
            continue
        if cond.type == "Complete":
            patch.status.update(phase="Succeeded", completedAt=now(), message="pg_dump completed")
            kopf.info(body, reason="BackupSucceeded", message=f"Wrote {status['backupFile']}")
        elif cond.type == "Failed":
            patch.status.update(phase="Failed", completedAt=now(), message=cond.message or cond.reason)
            kopf.warn(body, reason="BackupFailed", message=cond.message or cond.reason)
