"""Offline Blob -> per-cycle Table migration. Never imports app or sends SMS."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from azure.identity import AzureCliCredential
from azure.storage.blob import BlobClient
from azure.data.tables import TableClient
from table_state import TableStore


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account',required=True)
    parser.add_argument('--partition',required=True)
    parser.add_argument('--container',default='laundry-notifications')
    parser.add_argument('--table',default='LaundryState')
    parser.add_argument('--resource-group',default='laundry')
    parser.add_argument('--web',default='laundry-monitor')
    parser.add_argument('--function',default='laundry-writer-wally')
    parser.add_argument('--backup',type=Path)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    credential=AzureCliCredential()
    blob=BlobClient(f'https://{args.account}.blob.core.windows.net',args.container,
                    'recipient-state.json',credential=credential,retry_total=0)
    downloaded=blob.download_blob();raw=downloaded.readall();source_etag=downloaded.properties.etag
    state=json.loads(raw)
    print(json.dumps({'cycles':len(state['cycles']),'source_bytes':len(raw),
                      'sha256':hashlib.sha256(raw).hexdigest(),'apply':args.apply}))
    if not args.apply:
        return
    if not args.backup:
        raise SystemExit('--backup is required for migration')
    for kind,name in [('webapp',args.web),('functionapp',args.function)]:
        info=json.loads(subprocess.check_output(['az',kind,'show','-g',args.resource_group,'-n',name,
                                               '-o','json'],text=True))
        # CLI versions expose Flex apps as raw ARM properties, Web apps flattened.
        status=info.get('state') or info.get('properties',{}).get('state')
        if status!='Stopped':
            raise SystemExit(f'{name} must be stopped before migration')
    # Exclusive backup: never overwrite an earlier recovery snapshot.
    args.backup.parent.mkdir(parents=True,exist_ok=True)
    descriptor=os.open(args.backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(descriptor,'wb') as stream:
        stream.write(raw);stream.flush();os.fsync(stream.fileno())
    if blob.get_blob_properties().etag!=source_etag:
        raise SystemExit('Blob changed during preparation; migration not started')
    client=TableClient(f'https://{args.account}.table.core.windows.net',args.table,
                       credential=credential,retry_total=0)
    store=TableStore(lambda:client,args.partition)
    count=store.import_legacy(state,source_etag)
    if blob.get_blob_properties().etag!=source_etag:
        from azure.core import MatchConditions
        from azure.data.tables import UpdateMode
        root=client.get_entity(args.partition,'settings');root['Ready']=False
        client.update_entity(root,mode=UpdateMode.REPLACE,etag=root.metadata['etag'],
                             match_condition=MatchConditions.IfNotModified)
        raise SystemExit('Blob changed during migration; Table was fenced. Keep apps stopped.')
    print(json.dumps({'verified_cycles':count,'state_equal':store.snapshot()==state,
                      'table':args.table,'migrated_at':datetime.now(timezone.utc).isoformat()}))


if __name__=='__main__':
    main()
