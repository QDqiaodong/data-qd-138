"""Bounded binary raster component labeling and object geometry.

The package also provides a persistent correction ledger (see
:mod:`raster_objects.ledger`) with a command line at
:mod:`raster_objects.ledger_cli`, an activity-aware multi-source tile
mosaic warehouse (see :mod:`raster_objects.mosaic`) with a command line at
:mod:`raster_objects.mosaic_cli`, and a persistent blind double-annotation
collection pipeline (see :mod:`raster_objects.collection`) with a command
line at :mod:`raster_objects.collection_cli`.
"""
from .collection import (
    Collection,
    CollectionBusy,
    CollectionError,
    LeaseConflict,
    LeaseExpired,
    StaleClock,
    UnknownLease,
    WorkerDisqualified,
    collection_status,
    claim_work,
    export_dataset,
    init_collection,
    submit_answer,
    validate_answer,
    validate_campaign,
)
from .ledger import (
    Ledger,
    LedgerBusy,
    LedgerError,
    RequestConflict,
    RequestIdConflict,
    UnknownVersion,
    apply_request,
    export_version,
    init_ledger,
    normalize_request,
)
from .matching import match_objects
from .model import analyze
from .mosaic import (
    Mosaic,
    MosaicBusy,
    MosaicError,
    TileConflict,
    UnknownTile,
    init_mosaic,
    put_tile,
    window_query,
    withdraw_tile,
)

__all__ = [
    'Collection',
    'CollectionBusy',
    'CollectionError',
    'Ledger',
    'LedgerBusy',
    'LedgerError',
    'LeaseConflict',
    'LeaseExpired',
    'Mosaic',
    'MosaicBusy',
    'MosaicError',
    'RequestConflict',
    'RequestIdConflict',
    'StaleClock',
    'TileConflict',
    'UnknownLease',
    'UnknownTile',
    'UnknownVersion',
    'WorkerDisqualified',
    'analyze',
    'apply_request',
    'claim_work',
    'collection_status',
    'export_dataset',
    'export_version',
    'init_collection',
    'init_ledger',
    'init_mosaic',
    'match_objects',
    'normalize_request',
    'put_tile',
    'submit_answer',
    'validate_answer',
    'validate_campaign',
    'window_query',
    'withdraw_tile',
]
