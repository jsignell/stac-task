import argparse
import asyncio
import itertools
import json
import logging
import sys
from abc import ABC, abstractmethod
from copy import deepcopy
from os import makedirs
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp
from typing import Dict, List, Optional, Union

from boto3utils import s3
from pystac import ItemCollection

from .asset_io import download_item_assets, upload_item_assets_to_s3
from .exceptions import FailedValidation
from .utils import stac_jsonpath_match

# types
PathLike = Union[str, Path]
"""
Tasks can use parameters provided in a `process` Dictionary that is supplied in the ItemCollection
JSON under the "process" field. An example process definition:

```
{
    "description": "My process configuration"
    "upload_options": {
        "path_template": "s3://my-bucket/${collection}/${year}/${month}/${day}/${id}",
        "collections": {
            "landsat-c2l2": ""
        }
    },
    "tasks": {
        "task-name": {
            "param": "value"
        }
    }
}
```
"""


class Task(ABC):

    name = "task"
    description = "A task for doing things"
    version = "0.1.0"

    def __init__(
        self: "Task",
        item_collection: Dict,
        workdir: Optional[PathLike] = None,
        save_workdir: Optional[bool] = False,
        skip_upload: Optional[bool] = False,
        skip_validation: Optional[bool] = False,
    ):
        # set up logger
        self.logger = logging.getLogger(self.name)

        # set this to avoid confusion in destructor if called during validation
        self._save_workdir = True

        # validate input payload...or not
        if not skip_validation:
            if not self.validate(item_collection):
                raise FailedValidation()

        # set instance variables
        self._save_workdir = save_workdir
        self._skip_upload = skip_upload
        self._item_collection = item_collection

        # create temporary work directory if workdir is None
        if workdir is None:
            self._workdir = Path(mkdtemp())
        else:
            self._workdir = Path(workdir)
            makedirs(self._workdir, exist_ok=True)

    def __del__(self):
        # remove work directory if not running locally
        if not self._save_workdir:
            self.logger.debug("Removing work directory %s", self._workdir)
            rmtree(self._workdir)

    @property
    def process_definition(self) -> Dict:
        return self._item_collection.get("process", {})

    @property
    def parameters(self) -> Dict:
        return self.process_definition.get("tasks", {}).get(self.name, {})

    @property
    def upload_options(self) -> Dict:
        return self.process_definition.get("upload_options", {})

    @property
    def items_as_dicts(self) -> List[Dict]:
        return self._item_collection["features"]

    @property
    def items(self) -> ItemCollection:
        items_dict = {"type": "FeatureCollection", "features": self.items_as_dicts}
        return ItemCollection.from_dict(items_dict, preserve_dict=True)

    @classmethod
    def validate(cls, payload) -> bool:
        # put validation logic on input Items and process definition here
        return True

    @classmethod
    def add_software_version(cls, items):
        processing_ext = (
            "https://stac-extensions.github.io/processing/v1.1.0/schema.json"
        )
        for i in items:
            i["stac_extensions"].append(processing_ext)
            i["stac_extensions"] = list(set(i["stac_extensions"]))
            i["properties"]["processing:software"] = {cls.name: cls.version}
        return items

    def assign_collections(self):
        """Assigns new collection names based on"""
        for i, (coll, expr) in itertools.product(
            self._item_collection["features"],
            self.upload_options.get("collections", dict()).items(),
        ):
            if stac_jsonpath_match(i, expr):
                i["collection"] = coll

    def download_item_assets(
        self, item: Dict, path_template="${collection}/${id}", **kwargs
    ):
        """Download provided asset keys for all items in payload. Assets are saved in workdir in a
           directory named by the Item ID, and the items are updated with the new asset hrefs.

        Args:
            assets (Optional[List[str]], optional): List of asset keys to download. Defaults to all assets.
        """
        outdir = str(self._workdir / path_template)
        item = asyncio.run(download_item_assets(item, path_template=outdir, **kwargs))
        return item

    def upload_item_assets_to_s3(self, item: Dict, assets: Optional[List[str]] = None):
        if self._skip_upload:
            self.logger.warn("Skipping upload of new and modified assets")
            return item
        item = upload_item_assets_to_s3(item, assets=assets, **self.upload_options)
        return item

    # this should be in PySTAC
    @staticmethod
    def create_item_from_item(item):
        new_item = deepcopy(item)
        # create a derived output item
        links = [link["href"] for link in item["links"] if link["rel"] == "self"]
        if len(links) == 1:
            # add derived from link
            new_item["links"].append(
                {
                    "title": "Source STAC Item",
                    "rel": "derived_from",
                    "href": links[0],
                    "type": "application/json",
                }
            )
        return new_item

    @abstractmethod
    def process(self, **kwargs) -> List[Dict]:
        """Main task logic - virtual

        Returns:
            [type]: [description]
        """
        # download assets of interest, this will update self.items
        # self.download_assets(['key1', 'key2'])
        # do some stuff
        # self.upload_assets(['key1', 'key2'])
        return self.items

    @classmethod
    def handler(cls, payload, **kwargs):
        task = cls(payload, **kwargs)
        try:
            items = task.process(**task.parameters)
            task._item_collection["features"] = cls.add_software_version(items)
            task.assign_collections()
            with open(task._workdir / "stac.json", "w") as f:
                f.write(json.dumps(task._item_collection))
            return task._item_collection
        except Exception as err:
            task.logger.error(err, exc_info=True)
            raise err

    @classmethod
    def get_cli_parser(cls):
        """Parse CLI arguments"""
        dhf = argparse.ArgumentDefaultsHelpFormatter
        parser0 = argparse.ArgumentParser(description=cls.description)
        parser0.add_argument(
            "--version",
            help="Print version and exit",
            action="version",
            version=cls.version,
        )

        pparser = argparse.ArgumentParser(add_help=False)
        pparser.add_argument(
            "--logging", default="INFO", help="DEBUG, INFO, WARN, ERROR, CRITICAL"
        )

        subparsers = parser0.add_subparsers(dest="command")

        # run
        h = "Process STAC Item Collection"
        parser = subparsers.add_parser(
            "run", parents=[pparser], help=h, formatter_class=dhf
        )
        parser.add_argument(
            "input", help="Full path of item collection to process (s3 or local)"
        )
        h = "Use this as work directory. Will be created but not deleted)"
        parser.add_argument("--workdir", help=h, default=None, type=Path)
        h = "Save workdir after completion"
        parser.add_argument(
            "--save-workdir", dest="save_workdir", action="store_true", default=False
        )
        h = "Skip uploading of any generated assets and resulting STAC Items"
        parser.add_argument(
            "--skip-upload", dest="skip_upload", action="store_true", default=False
        )
        h = "Skip validation of input payload"
        parser.add_argument(
            "--skip-validation",
            dest="skip_validation",
            action="store_true",
            default=False,
        )
        h = "Run local mode (save-workdir, skip-upload, skip-validation set to True)"
        parser.add_argument("--local", action="store_true", default=False)
        return parser0

    @classmethod
    def parse_args(cls, args, parser=None):
        if parser is None:
            parser = cls.get_cli_parser()
        # turn Namespace into dictionary
        pargs = vars(parser.parse_args(args))
        # only keep keys that are not None
        pargs = {k: v for k, v in pargs.items() if v is not None}

        if pargs.get("local"):
            # local mode sets all of
            for k in ["save_workdir", "skip_upload", "skip_validation"]:
                pargs[k] = True

        if pargs.get("command", None) is None:
            parser.print_help()
            sys.exit(0)

        return pargs

    @classmethod
    def cli(cls, parser=None):
        args = cls.parse_args(sys.argv[1:], parser=parser)
        cmd = args.pop("command")

        # logging
        loglevel = args.pop("logging")
        logging.basicConfig(level=loglevel)

        # quiet these loud loggers
        quiet_loggers = ["botocore", "s3transfer", "urllib3"]
        for ql in quiet_loggers:
            logging.getLogger(ql).propagate = False

        if cmd == "run":
            href = args.pop("input")
            if href.startswith("s3://"):
                item_collection = s3().read_json(href)
            else:
                # open local item collection
                with open(href) as f:
                    item_collection = json.loads(f.read())
            # run task handler
            cls.handler(item_collection, **args)


# from https://pythonalgos.com/runtimeerror-event-loop-is-closed-asyncio-fix/
"""fix yelling at me error"""
from asyncio.proactor_events import _ProactorBasePipeTransport  # noqa
from functools import wraps  # noqa


def silence_event_loop_closed(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except RuntimeError as e:
            if str(e) != "Event loop is closed":
                raise

    return wrapper


_ProactorBasePipeTransport.__del__ = silence_event_loop_closed(
    _ProactorBasePipeTransport.__del__
)
"""fix yelling at me error end"""
