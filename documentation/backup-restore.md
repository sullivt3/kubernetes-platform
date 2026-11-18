# Backup and restore

Open **Operations → Backup & restore**. The **Cluster** tab uses the selected
organization, project and Kubernetes cluster. The **Platform** tab is available
to platform administrators and covers the entire management database.

## Platform archives

1. On a trusted recovery host run `age-keygen -o recovery-identity.txt`. Keep the
   identity in protected off-host storage. Obtain the public recipient with
   `age-keygen -y recovery-identity.txt`.
2. Configure the API and worker with the same absolute `BACKUP_DIRECTORY` and
   public `BACKUP_RECIPIENT`. Give the worker write access and the API read
   access. Use a private, trusted filesystem that supports hard links. The Compose
   stack provides a shared volume at `/var/lib/kplatform/backups`. Restart the
   services after configuration changes.
3. Choose **Create platform backup** and open its operation. Once it succeeds,
   download the encrypted archive and keep a copy off-host. Restrict writes to
   the archive repository. Only restore archives from a trusted source because
   PostgreSQL restores execute SQL from the archive.
4. To enable **Restore copy**, set `BACKUP_IDENTITY_FILE` and
   `RECOVERY_KEY_DIRECTORY` in the API and worker configuration. Mount the
   identity and a durable, private, writable key output directory on the worker.
   The restore role requires PostgreSQL database creation privileges. Enter the
   selected archive ID to confirm. The resulting operation identifies the new
   database and the key file in the configured output directory.

The worker uses a consistent PostgreSQL custom-format dump and age encryption.
It includes all organizations, resources, audit and operation history, encrypted
credentials, and the complete ordered credential encryption keyring. Temporary
plaintext requires private working storage. Allow disk space for the dump, tar,
and archive. For native deployments install age, pg_dump and pg_restore on the
worker and recovery host. Use PostgreSQL tools compatible with the source
server. The packaged worker includes version 17 tools.

## Recover with the management platform offline

Use the backend package from the backed-up deployment. Set `DATABASE_URL` in the
recovery shell to a PostgreSQL server that can create databases. Keep credentials
out of shell history. Run the following with a new database name and key output
file on a private filesystem.

```sh
python -m app.cli restore-backup backup.age \
  --database kplatform_recovered \
  --identity /secure/recovery-identity.txt \
  --key-output /secure/kplatform-recovered.env
```

The restore checks archive integrity and verifies decryption of every stored
credential in a staging database before publishing the recovered database.
Browser sessions and incomplete logins are discarded. Interrupted operations
are marked cancelled so workers cannot automatically replay infrastructure
changes. Review those operations and reconcile their targets before retrying.

For cutover, stop the old API, workers and scheduler. Configure the replacement
deployment with the recovered database, the recovered `SECRET_ENCRYPTION_KEYS`,
and a fresh Redis instance or dedicated empty Redis database. Restore the
deployment's OIDC configuration and access to its identity provider. Start the
API against the compatible schema, run `python -m app.cli check`, verify login,
inventory and audit history, then start workers and the scheduler and check
readiness again. The archive covers platform PostgreSQL state and credential
keys. Identity-provider data, PostgreSQL roles, deployment configuration, Redis,
VM disks and external storage have their own recovery procedures.

## Kubernetes cluster recovery

Install a compatible Velero controller in the `velero` namespace, configure an
off-cluster backup repository with its storage provider plugin, and wait for a
writable BackupStorageLocation to become Available. Cluster credentials need
list/get access to Backup, Restore, and BackupStorageLocation, and create access
on Backup and Restore for writes. The app lists request phases and warning/error
counts reported by Velero. Platform viewers can inspect inventory. Cluster
operators can submit backups and restores. [Velero backup storage](https://velero.io/docs/main/locations/)

For disaster recovery, provision a working destination cluster and install Velero
with access to the original repository. A read-only backup storage location
supports restore. Register that cluster in the platform, select a synchronized
Completed backup, name the restore, and enter the destination cluster name.
Existing resources follow Velero's skip policy. Review Velero warnings and
verify workload readiness and application data before cutover. Volume coverage
depends on the configured snapshot provider or file-system backups and pod
selection. Validate the data path with a restore drill for each storage class.
These workflows restore Kubernetes resources and eligible volumes. Cluster host
files, etcd disaster snapshots and external databases require separate backups.
[Velero disaster recovery](https://velero.io/docs/main/disaster-case/),
[Velero file-system backup](https://velero.io/docs/main/file-system-backup/)
