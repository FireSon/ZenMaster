"""Update platform for the ZenMaster integration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import EntityDevice, HaEntity


async def async_setup_entry(_hass: HomeAssistant, _config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the updates."""
    HaUpdate.add = async_add_entities


class HaUpdate(UpdateEntity, HaEntity):
    # a firmware update as an HA update: the device sets the versions and the progress, installing runs the callback

    add: AddEntitiesCallback
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS

    def __init__(self, device: EntityDevice, key: str, install: Callable) -> None:
        super().__init__(device, key, None)
        self._install = install
        device.add_entity(self)

    def show(self, installed: str | None, latest: str | None, summary: str | None = None, progress: int | None = None, busy: bool = False) -> None:
        self._attr_installed_version, self._attr_latest_version, self._attr_release_summary = installed, latest, summary
        self._attr_in_progress, self._attr_update_percentage = busy, progress
        if self.hass and self.hass.loop.is_running():
            self.schedule_update_ha_state()

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        await self._invoke(self._install, self)
