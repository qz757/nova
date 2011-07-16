# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2011 Zadara Storage Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

""" The virtul storage array extension"""


from webob import exc

from nova import vsa
from nova import volume
from nova import db
from nova import quota
from nova import exception
from nova import log as logging
from nova.api.openstack import common
from nova.api.openstack import extensions
from nova.api.openstack import faults
from nova.api.openstack import wsgi
from nova.api.openstack.contrib import volumes
from nova.compute import instance_types

from nova import flags
FLAGS = flags.FLAGS

LOG = logging.getLogger("nova.api.vsa")


class VsaController(object):
    """The Virtual Storage Array API controller for the OpenStack API."""

    _serialization_metadata = {
        'application/xml': {
            "attributes": {
                "vsa": [
                    "id",
                    "name",
                    "displayName",
                    "displayDescription",
                    "createTime",
                    "status",
                    "vcType",
                    "vcCount",
                    "driveCount",
                    ]}}}

    def __init__(self):
        self.vsa_api = vsa.API()
        super(VsaController, self).__init__()

    def _vsa_view(self, context, vsa, details=False):
        """Map keys for vsa summary/detailed view."""
        d = {}

        d['id'] = vsa['id']
        d['name'] = vsa['name']
        d['displayName'] = vsa['display_name']
        d['displayDescription'] = vsa['display_description']

        d['createTime'] = vsa['created_at']
        d['status'] = vsa['status']

        if vsa['vsa_instance_type']:
            d['vcType'] = vsa['vsa_instance_type'].get('name', None)
        else:
            d['vcType'] = None

        d['vcCount'] = vsa['vc_count']
        d['driveCount'] = vsa['vol_count']

        return d

    def _items(self, req, details):
        """Return summary or detailed list of VSAs."""
        context = req.environ['nova.context']
        vsas = self.vsa_api.get_all(context)
        limited_list = common.limited(vsas, req)
        res = [self._vsa_view(context, vsa, details) for vsa in limited_list]
        return {'vsaSet': res}

    def index(self, req):
        """Return a short list of VSAs."""
        return self._items(req, details=False)

    def detail(self, req):
        """Return a detailed list of VSAs."""
        return self._items(req, details=True)

    def show(self, req, id):
        """Return data about the given VSA."""
        context = req.environ['nova.context']

        try:
            vsa = self.vsa_api.get(context, vsa_id=id)
        except exception.NotFound:
            return faults.Fault(exc.HTTPNotFound())

        return {'vsa': self._vsa_view(context, vsa, details=True)}

    def create(self, req, body):
        """Create a new VSA."""
        context = req.environ['nova.context']

        if not body:
            return faults.Fault(exc.HTTPUnprocessableEntity())

        vsa = body['vsa']

        display_name = vsa.get('displayName')
        display_description = vsa.get('displayDescription')
        storage = vsa.get('storage')
        shared = vsa.get('shared')
        vc_type = vsa.get('vcType', FLAGS.default_vsa_instance_type)
        availability_zone = vsa.get('placement', {}).get('AvailabilityZone')

        try:
            instance_type = instance_types.get_instance_type_by_name(vc_type)
        except exception.NotFound:
            return faults.Fault(exc.HTTPNotFound())

        LOG.audit(_("Create VSA %(display_name)s of type %(vc_type)s"),
                    locals(), context=context)

        result = self.vsa_api.create(context,
                                    display_name=display_name,
                                    display_description=display_description,
                                    storage=storage,
                                    shared=shared,
                                    instance_type=instance_type,
                                    availability_zone=availability_zone)

        return {'vsa': self._vsa_view(context, result, details=True)}

    def delete(self, req, id):
        """Delete a VSA."""
        context = req.environ['nova.context']

        LOG.audit(_("Delete VSA with id: %s"), id, context=context)

        try:
            self.vsa_api.delete(context, vsa_id=id)
        except exception.NotFound:
            return faults.Fault(exc.HTTPNotFound())
        return exc.HTTPAccepted()


class VsaVolumeDriveController(volumes.VolumeController):
    """The base class for VSA volumes & drives.

    A child resource of the VSA object. Allows operations with
    volumes and drives created to/from particular VSA

    """

    _serialization_metadata = {
        'application/xml': {
            "attributes": {
                "volume": [
                    "id",
                    "name",
                    "status",
                    "size",
                    "availabilityZone",
                    "createdAt",
                    "displayName",
                    "displayDescription",
                    "vsaId",
                    ]}}}

    def __init__(self):
        # self.compute_api = compute.API()
        # self.vsa_api = vsa.API()
        self.volume_api = volume.API()
        super(VsaVolumeDriveController, self).__init__()

    def _translation(self, context, vol, vsa_id, details):
        if details:
            translation = volumes.translate_volume_detail_view
        else:
            translation = volumes.translate_volume_summary_view

        d = translation(context, vol)
        d['vsaId'] = vol[self.direction]
        return d

    def _check_volume_ownership(self, context, vsa_id, id):
        obj = self.object
        try:
            volume_ref = self.volume_api.get(context, volume_id=id)
        except exception.NotFound:
            LOG.error(_("%(obj)s with ID %(id)s not found"), locals())
            raise

        own_vsa_id = volume_ref[self.direction]
        if  own_vsa_id != int(vsa_id):
            LOG.error(_("%(obj)s with ID %(id)s belongs to VSA %(own_vsa_id)s"\
                        " and not to VSA %(vsa_id)s."), locals())
            raise exception.Invalid()

    def _items(self, req, vsa_id, details):
        """Return summary or detailed list of volumes for particular VSA."""
        context = req.environ['nova.context']

        vols = self.volume_api.get_all_by_vsa(context, vsa_id,
                            self.direction.split('_')[0])
        limited_list = common.limited(vols, req)

        res = [self._translation(context, vol, vsa_id, details) \
               for vol in limited_list]

        return {self.objects: res}

    def index(self, req, vsa_id):
        """Return a short list of volumes created from particular VSA."""
        LOG.audit(_("Index. vsa_id=%(vsa_id)s"), locals())
        return self._items(req, vsa_id, details=False)

    def detail(self, req, vsa_id):
        """Return a detailed list of volumes created from particular VSA."""
        LOG.audit(_("Detail. vsa_id=%(vsa_id)s"), locals())
        return self._items(req, vsa_id, details=True)

    def create(self, req, vsa_id, body):
        """Create a new volume from VSA."""
        LOG.audit(_("Create. vsa_id=%(vsa_id)s, body=%(body)s"), locals())
        context = req.environ['nova.context']

        if not body:
            return faults.Fault(exc.HTTPUnprocessableEntity())

        vol = body[self.object]
        size = vol['size']
        LOG.audit(_("Create volume of %(size)s GB from VSA ID %(vsa_id)s"),
                    locals(), context=context)

        new_volume = self.volume_api.create(context, size, None,
                                            vol.get('displayName'),
                                            vol.get('displayDescription'),
                                            from_vsa_id=vsa_id)

        return {self.object: self._translation(context, new_volume,
                                               vsa_id, True)}

    def update(self, req, vsa_id, id, body):
        """Update a volume."""
        context = req.environ['nova.context']

        try:
            self._check_volume_ownership(context, vsa_id, id)
        except exception.NotFound:
            return faults.Fault(exc.HTTPNotFound())
        except exception.Invalid:
            return faults.Fault(exc.HTTPBadRequest())

        vol = body[self.object]
        updatable_fields = ['display_name',
                            'display_description',
                            'status',
                            'provider_location',
                            'provider_auth']
        changes = {}
        for field in updatable_fields:
            if field in vol:
                changes[field] = vol[field]

        obj = self.object
        LOG.audit(_("Update %(obj)s with id: %(id)s, changes: %(changes)s"),
                    locals(), context=context)

        try:
            self.volume_api.update(context, volume_id=id, fields=changes)
        except exception.NotFound:
            return faults.Fault(exc.HTTPNotFound())
        return exc.HTTPAccepted()

    def delete(self, req, vsa_id, id):
        """Delete a volume."""
        context = req.environ['nova.context']

        LOG.audit(_("Delete. vsa_id=%(vsa_id)s, id=%(id)s"), locals())

        try:
            self._check_volume_ownership(context, vsa_id, id)
        except exception.NotFound:
            return faults.Fault(exc.HTTPNotFound())
        except exception.Invalid:
            return faults.Fault(exc.HTTPBadRequest())

        return super(VsaVolumeDriveController, self).delete(req, id)

    def show(self, req, vsa_id, id):
        """Return data about the given volume."""
        context = req.environ['nova.context']

        LOG.audit(_("Show. vsa_id=%(vsa_id)s, id=%(id)s"), locals())

        try:
            self._check_volume_ownership(context, vsa_id, id)
        except exception.NotFound:
            return faults.Fault(exc.HTTPNotFound())
        except exception.Invalid:
            return faults.Fault(exc.HTTPBadRequest())

        return super(VsaVolumeDriveController, self).show(req, id)


class VsaVolumeController(VsaVolumeDriveController):
    """The VSA volume API controller for the Openstack API.

    A child resource of the VSA object. Allows operations with volumes created
    by particular VSA

    """

    def __init__(self):
        self.direction = 'from_vsa_id'
        self.objects = 'volumes'
        self.object = 'volume'
        super(VsaVolumeController, self).__init__()


class VsaDriveController(VsaVolumeDriveController):
    """The VSA Drive API controller for the Openstack API.

    A child resource of the VSA object. Allows operations with drives created
    for particular VSA

    """

    def __init__(self):
        self.direction = 'to_vsa_id'
        self.objects = 'drives'
        self.object = 'drive'
        super(VsaDriveController, self).__init__()

    def create(self, req, vsa_id, body):
        """Create a new drive for VSA. Should be done through VSA APIs"""
        return faults.Fault(exc.HTTPBadRequest())

    def update(self, req, vsa_id, id, body):
        """Update a drive. Should be done through VSA APIs"""
        return faults.Fault(exc.HTTPBadRequest())


class VsaVPoolController(object):
    """The vPool VSA API controller for the OpenStack API."""

    _serialization_metadata = {
        'application/xml': {
            "attributes": {
                "vpool": [
                    "id",
                    "vsaId",
                    "name",
                    "displayName",
                    "displayDescription",
                    "driveCount",
                    "driveIds",
                    "protection",
                    "stripeSize",
                    "stripeWidth",
                    "createTime",
                    "status",
                    ]}}}

    def __init__(self):
        self.vsa_api = vsa.API()
        super(VsaVPoolController, self).__init__()

    def index(self, req, vsa_id):
        """Return a short list of vpools created from particular VSA."""
        return {'vpools': []}

    def create(self, req, vsa_id, body):
        """Create a new vPool for VSA."""
        return faults.Fault(exc.HTTPBadRequest())

    def update(self, req, vsa_id, id, body):
        """Update vPool parameters."""
        return faults.Fault(exc.HTTPBadRequest())

    def delete(self, req, vsa_id, id):
        """Delete a vPool."""
        return faults.Fault(exc.HTTPBadRequest())

    def show(self, req, vsa_id, id):
        """Return data about the given vPool."""
        return faults.Fault(exc.HTTPBadRequest())


class Virtual_storage_arrays(extensions.ExtensionDescriptor):

    def get_name(self):
        return "VSAs"

    def get_alias(self):
        return "zadr-vsa"

    def get_description(self):
        return "Virtual Storage Arrays support"

    def get_namespace(self):
        return "http://docs.openstack.org/ext/vsa/api/v1.1"

    def get_updated(self):
        return "2011-06-29T00:00:00+00:00"

    def get_resources(self):
        resources = []
        res = extensions.ResourceExtension(
                            'zadr-vsa',
                            VsaController(),
                            collection_actions={'detail': 'GET'},
                            member_actions={'add_capacity': 'POST',
                                            'remove_capacity': 'POST'})
        resources.append(res)

        res = extensions.ResourceExtension('volumes',
                            VsaVolumeController(),
                            collection_actions={'detail': 'GET'},
                            parent=dict(
                                member_name='vsa',
                                collection_name='zadr-vsa'))
        resources.append(res)

        res = extensions.ResourceExtension('drives',
                            VsaDriveController(),
                            collection_actions={'detail': 'GET'},
                            parent=dict(
                                member_name='vsa',
                                collection_name='zadr-vsa'))
        resources.append(res)

        res = extensions.ResourceExtension('vpools',
                            VsaVPoolController(),
                            parent=dict(
                                member_name='vsa',
                                collection_name='zadr-vsa'))
        resources.append(res)

        return resources
