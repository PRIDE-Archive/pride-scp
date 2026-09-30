# MassIVE staging with pridepy

PRIDE-SCP containers include `pridepy==0.0.16` for public repository transfer.
For MassIVE staging on HPC, prefer the project helper:

```bash
python /opt/pride-scp/scripts/download_massive_pridepy.py \
  --accession MSV000098940 \
  --output-root /nfs/research/juan/DIA/singj \
  --manifest-root /nfs/research/juan/DIA/singj/MSV000098940_transfer \
  --completion-root /nfs/research/juan/DIA/singj/MSV000098940_transfer \
  --parallel-files 3
```

The default `--transport https-index` asks pridepy's GNPS2 HTTPS dataset index
for the file inventory, then downloads from MassIVE's ProteoSAFe HTTPS endpoint.
This deliberately avoids a recursive FTPS tree walk, which can be slow for
large Bruker `.d` directory trees.

Use `--transport auto` only when the normal pridepy MassIVE behaviour is wanted:
FTPS discovery first, then HTTPS fallback if FTPS fails.

## Dataset lists

One accession per line is supported:

```text
# Leduc et al. 2025
MSV000098940
MSV0000XXXXX
```

Run with:

```bash
python /opt/pride-scp/scripts/download_massive_pridepy.py \
  --dataset-list massive_datasets.txt \
  --output-root /nfs/research/juan/DIA/singj \
  --manifest-root /nfs/research/juan/DIA/singj/massive_transfer_manifests \
  --completion-root /nfs/research/juan/DIA/singj/massive_transfer_manifests
```

The helper preserves each dataset's relative directory structure under
`<output-root>/<MSV accession>/`.

## Resume semantics

Do not use pridepy's `skip_if_downloaded_already=True` for this workflow.
The helper intentionally passes `False`: the HTTP transport can compare an
existing local file with the remote size and Range-resume partial files.
Blindly skipping existing paths would risk accepting an incomplete transfer.

## Pinning

`https-index` calls the private `MassiveProvider._list_via_https` API and
therefore verifies that the runtime contains exactly `pridepy==0.0.16`.
Review this helper when updating the pridepy pin.
