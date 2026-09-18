"""Background Jobs for Welcome Wizard."""

import contextlib
from collections import OrderedDict
from typing import Any

from nautobot.apps.jobs import Job, StringVar
from nautobot.core.celery import register_jobs
from nautobot.dcim.forms import DeviceTypeImportForm
from nautobot.dcim.models import (
    ConsolePortTemplate,
    ConsoleServerPortTemplate,
    DeviceBayTemplate,
    DeviceType,
    FrontPortTemplate,
    InterfaceTemplate,
    Manufacturer,
    ModuleBayTemplate,
    PowerOutletTemplate,
    PowerPortTemplate,
    RearPortTemplate,
)

from welcome_wizard.models.importer import DeviceTypeImport

COMPONENTS = OrderedDict()
COMPONENTS["console-ports"] = ConsolePortTemplate
COMPONENTS["console-server-ports"] = ConsoleServerPortTemplate
COMPONENTS["power-ports"] = PowerPortTemplate
COMPONENTS["power-outlets"] = PowerOutletTemplate
COMPONENTS["interfaces"] = InterfaceTemplate
COMPONENTS["rear-ports"] = RearPortTemplate
COMPONENTS["front-ports"] = FrontPortTemplate
COMPONENTS["device-bays"] = DeviceBayTemplate
COMPONENTS["module-bays"] = ModuleBayTemplate

STRIP_KEYWORDS = {
    "interfaces": ["poe_mode", "poe_type"],
}

RENAME_COMPONENT_PARAMS = {
    "power-outlets": {
        "power_port": "power_port_template",
    },
    "front-ports": {
        "rear_port": "rear_port_template",
    },
}

# Component groups whose items reference a sibling template by name, keyed to the FK
# field (post-rename) and the model to resolve it against.
FK_PARENT_LOOKUP = {
    "power-outlets": ("power_port_template", PowerPortTemplate),
    "front-ports": ("rear_port_template", RearPortTemplate),
}


def import_device_type(data: dict[str, Any]) -> DeviceType:
    """Import DeviceType."""
    manufacturer = Manufacturer.objects.get(name=data.get("manufacturer"))
    model = data.get("model")
    with contextlib.suppress(DeviceType.DoesNotExist):
        devtype = DeviceType.objects.get(model=model, manufacturer=manufacturer)
        raise ValueError(
            f"Unable to import this device_type, a DeviceType with this model ({model}) and manufacturer ({manufacturer}) already exist."
        )
    dtif = DeviceTypeImportForm(data)
    devtype = dtif.save()

    # Import All Components
    for key, component_class in COMPONENTS.items():
        if key not in data:
            continue
        renames = RENAME_COMPONENT_PARAMS.get(key, {})
        fk_lookup = FK_PARENT_LOOKUP.get(key)
        component_list = []
        for raw_item in data[key]:
            component_kwargs = {k: v for k, v in raw_item.items() if k not in STRIP_KEYWORDS.get(key, [])}
            for legacy_key, renamed_key in renames.items():
                if legacy_key in component_kwargs:
                    component_kwargs[renamed_key] = component_kwargs.pop(legacy_key)
            if fk_lookup:
                fk_field, fk_model = fk_lookup
                nullable = fk_model is PowerPortTemplate
                parent_name = component_kwargs.get(fk_field)
                if parent_name is None and not nullable:
                    msg = f"Unable to import {key} item on {devtype}: missing required {fk_field!r} value."
                    raise ValueError(msg)
                try:
                    component_kwargs[fk_field] = fk_model.objects.get(device_type=devtype, name=parent_name)
                except fk_model.DoesNotExist:
                    if nullable:
                        component_kwargs[fk_field] = None
                    else:
                        msg = (
                            f"Unable to import {key} item on {devtype}: "
                            f"no {fk_model.__name__} named {parent_name!r} found."
                        )
                        raise ValueError(msg) from None
            component_list.append(component_class(device_type=devtype, **component_kwargs))
        component_class.objects.bulk_create(component_list)
    return devtype


name = "Welcome Wizard"  # pylint: disable=invalid-name


class WelcomeWizardImportManufacturer(Job):
    """Manufacturer Import."""

    class Meta:  # pylint: disable=too-few-public-methods
        """Meta for Manufacturer Import."""

        name = "Welcome Wizard - Import Manufacturer"
        description = "Imports a chosen Manufacturer (Run from the Welcome Wizard Dashboard)"

    manufacturer_name = StringVar(description="Name of the new manufacturer")

    def run(self, manufacturer_name):  # pylint: disable=arguments-differ
        """Tries to import the selected Manufacturer into Nautobot."""
        # Create the new manufacturer
        manufacturer, _ = Manufacturer.objects.update_or_create(
            name=manufacturer_name,
        )
        self.logger.info("Created new manufacturer", extra={"object": manufacturer})


class WelcomeWizardImportDeviceType(Job):
    """Device Type Import."""

    class Meta:  # pylint: disable=too-few-public-methods
        """Meta for Device Type Import."""

        name = "Welcome Wizard - Import Device Type"
        description = "Imports a chosen Device Type (Run from the Welcome Wizard Dashboard)"

    filename = StringVar()

    def run(self, filename):  # pylint: disable=arguments-differ
        """Tries to import the selected Device Type into Nautobot."""
        device_type = filename or "none.yaml"

        device_type_data = DeviceTypeImport.objects.filter(filename=device_type)[0].device_type_data

        manufacturer = device_type_data.get("manufacturer")
        Manufacturer.objects.update_or_create(
            name=manufacturer,
        )

        try:
            devtype = import_device_type(device_type_data)
        except ValueError as exc:
            self.logger.error(str(exc))
            raise exc

        self.logger.info(  # pylint: disable=logging-fstring-interpolation
            f"Imported DeviceType {device_type_data.get('model')} successfully", extra={"object": devtype}
        )


register_jobs(WelcomeWizardImportManufacturer, WelcomeWizardImportDeviceType)
