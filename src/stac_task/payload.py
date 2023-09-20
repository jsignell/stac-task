from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Type

import pystac.utils
import stac_asset.blocking
from pydantic import BaseModel, Field

import stac_task

from .models import Process
from .task import Input, Output, Task
from .types import PathLikeObject


class Payload(BaseModel):
    """A payload describing the items and the tasks to be executed

    Pretty specific to [cirrus](https://github.com/cirrus-geo/)."""

    type: Literal["FeatureCollection"] = "FeatureCollection"
    """Must be FeatureCollection."""

    features: List[Dict[str, Any]] = []
    """A list of STAC items, or things sort of like STAC items."""

    process: Process = Process()
    """The process definition."""

    # TODO do we need to support `url` as well?
    href: Optional[str] = None
    """An optional href parameter, used in indirect payloads.
    
    Indirect payloads contain an href to a large payload living on s3.
    """

    self_href: Optional[str] = Field(default=None, exclude=True)
    """The location that the payload was read from.
    
    Used to resolve relative hrefs.
    """

    @classmethod
    def from_href(cls, href: str, allow_indrections: bool = True) -> Payload:
        """Loads a payload from an href.

        If the payload has an `href` attribute set, that href will be fetched.
        This is used for "indirect" payloads that point to large payloads that
        need to be stored on s3.

        Args:
            href: The href to load the payload from.
            allow_indirections: Whether to follow indirection links. Generally
                used only when recursively calling this function to prevent infinite
                indirection loops.

        Returns:
            Payload: The payload
        """
        # TODO we could go async with these
        payload = cls.model_validate_json(stac_asset.blocking.read_href(href))
        if payload.href and not payload.features:
            if allow_indrections:
                href = pystac.utils.make_absolute_href(
                    payload.href, href, start_is_dir=False
                )
                return cls.from_href(href, allow_indrections=False)
            else:
                raise ValueError("Multiple indirections are not supported")
        else:
            payload.self_href = href
            return payload

    def execute(
        self, name: str, task_class: Optional[Type[Task[Input, Output]]] = None
    ) -> Payload:
        """Executes a task on this payload, returning the updated payload.

        The task must be registered via `stac_task.register_task("name", TaskClass)`.

        Args:
            name: The name of the task to execute
            task_class: The task class to insatiate and execute. If not
                provided, the class will be looked up in the registry.

        Returns:
            Payload: A new payload, with the output items
        """
        if name not in self.process.tasks:
            raise ValueError(f"task is not configured in payload: {name}")
        config = self.process.tasks[name]
        if not isinstance(config, dict):
            raise ValueError(f"task config is not a dict: {name} is a {type(config)}")
        if not task_class:
            task_class = stac_task.get_task(name)
        task = task_class(**config)
        task.payload_href = self.self_href
        features = task.process_dicts(self.features)
        payload = self.model_copy(deep=True, update={"features": features})
        return payload

    def to_path(self, path: PathLikeObject) -> None:
        """Writes a payload a path.

        Args:
            path: The path to write the payload to.
            Payload: A new payload, with the output items
        """
        Path(path).write_text(self.model_dump_json())