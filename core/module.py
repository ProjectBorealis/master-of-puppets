import json
import logging

import maya.cmds as cmds
import maya.api.OpenMaya as om2

from mop.core.fields import (
    EnumField,
    ObjectField,
    StringField,
    ObjectListField,
)
from mop.core.mopNode import MopNode
from mop.modules import all_rig_modules
from mop.utils.dg import find_mirror_node
import mop.attributes
import mop.dag
import mop.metadata
import mop.config

from shapeshifter import shapeshifter


logger = logging.getLogger(__name__)


class RigModule(MopNode):

    name = StringField(
        displayable=True,
        editable=True,
        gui_order=-2,  # make sure it's always on top
        unique=True,
        tooltip="Base name of the module"
    )
    side = EnumField(
        choices=['M', 'L', 'R'],
        displayable=True,
        editable=True,
        gui_order=-2,  # make sure it's always on top
        tooltip="Side of the module:\n"
        "M: Middle\n"
        "L: Left\n"
        "R: Right"
    )

    mirror_type = EnumField(
        choices=['Behavior', 'Orientation'],
        displayable=True,
        editable=True,
        gui_order=-1,  # make sure it's always on top
        tooltip="How to mirror the module."
    )

    _module_mirror = ObjectField()

    default_side = 'M'

    owned_nodes = ObjectListField()

    # Joint of the rig skeleton under which the deform joints will be parented.
    parent_joint = ObjectField()

    module_type = StringField()

    # group holding all this module's placement nodes
    placement_group = ObjectField()

    # group holding all this module's controls
    controls_group = ObjectField()

    # group holding all this module's extra stuff
    extras_group = ObjectField()

    # list of all of this module's deform joints
    deform_joints = ObjectListField()

    # list of all of this module's placement_locators
    placement_locators = ObjectListField()

    controllers = ObjectListField()

    def __init__(self, name, side='M', parent_joint=None, rig=None):
        if cmds.objExists(name):
            self.node_name = name
        else:
            metadata = {
                'base_name': name,
                'side': side,
                'role': 'mod',
            }
            self.node_name = mop.metadata.name_from_metadata(metadata)
        super(RigModule, self).__init__(self.node_name)

        self.rig = rig
        if not self.is_initialized.get():
            self.name.set(name)
            self.side.set(side)
            self.module_type.set(self.__class__.__name__)

            parent = cmds.listRelatives(self.node_name, parent=True)
            if not parent or parent[0] != rig.modules_group.get():
                cmds.parent(self.node_name, rig.modules_group.get())

            if parent_joint:
                self.parent_joint.set(parent_joint)
                mop.dag.matrix_constraint(parent_joint, self.node_name)

            self.initialize()
            self.place_placement_nodes()
            self.update()
            self.is_initialized.set(True)

    @property
    def parent_module(self):
        parent_joint = self.parent_joint.get()
        if parent_joint:
            parent_module = cmds.listConnections(parent_joint + '.module', source=True)[0]
            module_type = cmds.getAttr(parent_module + '.module_type')
            parent_module = all_rig_modules[module_type](parent_module, rig=self.rig)
            return parent_module

    @property
    def module_mirror(self):
        """Return the actual instance of the module mirror."""
        mirror_node = self._module_mirror.get()
        if mirror_node:
            mirror_module = all_rig_modules[self.module_type.get()](
                mirror_node,
                rig=self.rig
            )
            return mirror_module

    @module_mirror.setter
    def module_mirror(self, value):
        self._module_mirror.set(value)

    @property
    def is_mirrored(self):
        return bool(self.module_mirror)

    def initialize(self):
        """Creation of all the needed placement nodes.

        This must at least include all the module's deform joints.
        Some module may inclue placement locators as well.

        Will be called automatically when creating the module.
        You need to overwrite this method in your subclasses.
        """
        self.placement_group.set(
            self.add_node(
                'transform',
                'grp',
                description='placement',
                parent=self.node_name
            )
        )
        cmds.setAttr(self.placement_group.get() + '.inheritsTransform', False)
        self.controls_group.set(
            self.add_node(
                'transform',
                role='grp',
                description='controls',
                parent = self.node_name
            )
        )

        self.extras_group.set(
            self.add_node(
                'transform',
                role='grp',
                description='extras',
                parent = self.node_name
            )
        )
        cmds.setAttr(self.extras_group.get() + '.visibility', False)

    def place_placement_nodes(self):
        """Place the deform joints and placement locators based on the config."""
        deform_joint_matrices = mop.config.default_module_placement.get(
            self.__class__.__name__, {}
        ).get("deform_joints")
        for i, joint in enumerate(self.deform_joints):
            try:
                matrix = deform_joint_matrices[i]
            except Exception:
                logger.warning("No default matrix found of {}".format(joint))
            else:
                cmds.xform(joint, matrix=matrix, worldSpace=True)

        placement_locators_matrices = mop.config.default_module_placement.get(
            self.__class__.__name__, {}
        ).get("placement_locators")
        for i, joint in enumerate(self.placement_locators):
            try:
                matrix = placement_locators_matrices[i]
            except Exception:
                logger.warning("No default matrix found of {}".format(joint))
            else:
                cmds.xform(joint, matrix=matrix, worldSpace=True)

    def update(self):
        """Update the maya scene based on the module's fields

        This should ONLY be called in placement mode.
        """
        if self.is_built.get():
            return

        self.update_parent_joint()

        scene_metadata = mop.metadata.metadata_from_name(self.node_name)
        name_changed = self.name.get() != scene_metadata['base_name']
        side_changed = self.side.get() != scene_metadata['side']

        if name_changed or side_changed:
            # rename the module node
            new_name = self._update_node_name(self.node_name)
            self.node_name = new_name

            # rename the owned nodes
            for node in self.owned_nodes.get():
                self._update_node_name(node)

            # rename the persistent attributes
            persistent_attrs = cmds.listAttr(
                self.node_name,
                category='persistent_attribute_backup'
            )
            if persistent_attrs:
                for attr in persistent_attrs:
                    old_node, attr_name = attr.split('__')

                    metadata = mop.metadata.metadata_from_name(old_node)
                    metadata['base_name'] = self.name.get()
                    metadata['side'] = self.side.get()
                    new_node = mop.metadata.name_from_metadata(metadata)
                    logger.debug("Renaming persistent attribute from {} to {}".format(
                        self.node_name + '.' + attr,
                        self.node_name + '.' + new_node + '__' + attr_name
                    ))
                    cmds.renameAttr(
                        self.node_name + '.' + attr,
                        new_node + '__' + attr_name
                    )

    def update_parent_joint(self):
        # delete the old constraint
        old_constraint_nodes = []

        first_level_nodes = cmds.listConnections(
            self.node_name + '.translate',
            source=True
        ) or []
        old_constraint_nodes.extend(first_level_nodes)

        for node in first_level_nodes:
            second_level_nodes = cmds.listConnections(
                node + '.inputMatrix',
                source=True
            ) or []
            old_constraint_nodes.extend(second_level_nodes)

        if old_constraint_nodes:
            cmds.delete(old_constraint_nodes)

        parent = self.parent_joint.get()
        if parent:
            mop.dag.matrix_constraint(parent, self.node_name)

    def _update_node_name(self, node):
        metadata = mop.metadata.metadata_from_name(node)
        metadata['base_name'] = self.name.get()
        metadata['side'] = self.side.get()
        new_name = mop.metadata.name_from_metadata(metadata)
        cmds.rename(node, new_name)
        return new_name

    def _build(self):
        """Setup some stuff before actually building the module.

        Call this method instead of `build()` to make sure
        everything is setup properly
        """
        self.build()
        self.is_built.set(True)

    def build(self):
        """Actual rigging of the module.

        The end result should _always_ drive your module's deform joints
        You need to overwrite this method in your subclasses.
        """
        raise NotImplementedError

    def publish(self):
        """Clean the rig for the animation.

        Nothing in there should change how the rig works.
        It's meant to hide some stuff that the rigger would need after the build
        maybe lock some attributes, set some default values, etc.
        """
        cmds.setAttr(self.extras_group.get() + '.visibility', False)

    def add_node(
        self,
        node_type,
        role=None,
        object_id=None,
        description=None,
        *args,
        **kwargs
    ):
        """Add a node to this `MopNode`.

        args and kwargs will directly be passed to ``cmds.createNode()``

        :param node_type: type of the node to create, will be passed to ``cmds.createNode()``.
        :type node_type: str
        :param role: role of the node (this will be the last part of its name).
        :type role: str
        :param object_id: optional index for the node.
        :type object_id: int
        :param description: optional description for the node
        :type object_id: str
        """
        if not role:
            role = node_type
        metadata = {
            'base_name': self.name.get(),
            'side': self.side.get(),
            'role': role,
            'description': description,
            'id': object_id
        }
        name = mop.metadata.name_from_metadata(metadata)
        if cmds.objExists(name):
            raise ValueError("A node with the name `{}` already exists".format(name))
        if node_type == 'locator':
            node = cmds.spaceLocator(name=name)[0]
        else:
            node = cmds.createNode(node_type, name=name, *args, **kwargs)
        cmds.addAttr(
            node,
            longName='module',
            attributeType = 'message'
        )
        cmds.connectAttr(
            self.node_name + '.message',
            node + '.module'
        )
        self.owned_nodes.append(node)
        return node

    def _add_deform_joint(
        self,
        parent=None,
        object_id=None,
        description=None,
    ):
        """Creates a new deform joint for this module.

        Args:
            parent (str): node under which the new joint will be parented
        """
        if object_id is None:
            object_id = len(self.deform_joints)

        new_joint = self.add_node(
            'joint',
            role='deform',
            object_id=object_id,
            description=description
        )

        if not parent:
            parent = self.parent_joint.get()
        if not parent:
            parent = self.rig.skeleton_group.get()

        cmds.parent(new_joint, parent)

        for transform in ['translate', 'rotate', 'scale', 'jointOrient']:
            if transform == 'scale':
                value = 1
            else:
                value = 0
            for axis in 'XYZ':
                attr = transform + axis
                cmds.setAttr(new_joint + '.' + attr, value)

        self.deform_joints.append(new_joint)
        return new_joint

    def _add_placement_locator(self, description=None, object_id=None, parent=None):
        """Creates a new placement locator for this module.

        A placement locator is a way to get placement data without polluting
        the deform skeleton.
        """
        locator = self.add_node(
            'locator',
            role='placement',
            object_id=object_id,
            description=description
        )
        if not parent:
            parent = self.placement_group.get()
        cmds.parent(locator, parent)

        for transform in ['translate', 'rotate', 'scale']:
            if transform == 'scale':
                value = 1
            else:
                value = 0
            for axis in 'XYZ':
                attr = transform + axis
                cmds.setAttr(locator + '.' + attr, value)

        self.placement_locators.append(locator)
        return locator

    def add_control(
        self,
        dag_node,
        object_id=None,
        description=None,
        shape_type='circle'
    ):
        metadata = mop.metadata.metadata_from_name(dag_node)
        if object_id is not None:
            metadata['id'] = object_id
        if description is not None:
            metadata['description'] = description
        metadata['role'] = 'ctl'
        ctl_name = mop.metadata.name_from_metadata(metadata)
        ctl = shapeshifter.create_controller_from_name(shape_type)
        ctl = cmds.rename(ctl, ctl_name)

        # update the controller color based on its side
        current_data = shapeshifter.get_shape_data(ctl)
        new_data = []
        side_color = mop.config.side_color[self.side.get()]
        for shape_data in current_data:
            new_shape_data = shape_data.copy()
            new_shape_data['enable_overrides'] = True
            new_shape_data['use_rgb'] = True
            new_shape_data['color_rgb'] = side_color
            new_data.append(new_shape_data)
        shapeshifter.change_controller_shape(ctl, new_data)

        # get the existing shape data if it exists
        mop.attributes.create_persistent_attribute(
            ctl,
            self.node_name,
            longName='shape_data',
            dataType='string'
        )
        ctl_data = cmds.getAttr(ctl + '.shape_data')
        if ctl_data:
            ctl_data = json.loads(ctl_data)
            shapeshifter.change_controller_shape(ctl, ctl_data)

        mop.attributes.create_persistent_attribute(
            ctl,
            self.node_name,
            longName='attributes_state',
            dataType='string'
        )

        mop.attributes.create_persistent_attribute(
            ctl,
            self.node_name,
            longName='parent_space_data',
            dataType='string',
        )

        # We cannot set a default value on strings, so set the persistent
        # attribute after its creation.
        # It is mandatory to set a default value here, without a value
        # the attribute returns `None` when rebuilt and this crashes
        # the `setAttr` command.
        if not cmds.getAttr(ctl + '.parent_space_data'):
            cmds.setAttr(ctl + '.parent_space_data', '{}', type='string')

        mop.dag.snap_first_to_last(ctl, dag_node)
        parent_group = mop.dag.add_parent_group(ctl, 'buffer')
        self.controllers.append(ctl)
        return ctl, parent_group

    def find_non_mirrored_parents(self, non_mirrored_parents=None):
        """Recursively find the parent module that are not mirrored."""

        if non_mirrored_parents is None:
            non_mirrored_parents = []

        parent = self.parent_module

        if not parent.module_mirror and parent.side.get() != 'M':
            non_mirrored_parents.append(parent)
            RigModule.find_non_mirrored_parents(parent, non_mirrored_parents)

        return non_mirrored_parents
    
    def update_mirror(self):

        # update all the fields to match the mirror module
        for field in self.module_mirror.fields:
            if field.name in ['name', 'side']:
                continue
            if field.editable:
                value = None
                if isinstance(field, ObjectField):
                    orig_value = getattr(self.module_mirror, field.name).get()
                    value = find_mirror_node(orig_value)
                elif isinstance(field, ObjectListField):
                    orig_value = getattr(self.module_mirror, field.name).get()
                    value = [find_mirror_node(v) for v in orig_value]
                else:
                    value = getattr(self.module_mirror, field.name).get()

                if value:
                    getattr(self, field.name).set(value)

        self.update()

        # mirror the nodes based on the mirror type
        orig_nodes = self.module_mirror.deform_joints.get() + self.module_mirror.placement_locators.get()
        new_nodes = self.deform_joints.get() + self.placement_locators.get()
        mirror_type = self.mirror_type.get()
        for orig_node, new_node in zip(orig_nodes, new_nodes):
            if mirror_type.lower() == 'behavior':
                world_reflexion_mat = om2.MMatrix([
                    -1.0, -0.0, -0.0, 0.0,
                     0.0,  1.0,  0.0, 0.0,
                     0.0,  0.0,  1.0, 0.0,
                     0.0,  0.0,  0.0, 1.0
                ])
                local_reflexion_mat = om2.MMatrix([
                    -1.0,  0.0,  0.0, 0.0,
                     0.0, -1.0,  0.0, 0.0,
                     0.0,  0.0, -1.0, 0.0,
                     0.0,  0.0,  0.0, 1.0
                ])
                orig_node_mat = om2.MMatrix(
                    cmds.getAttr(orig_node + '.worldMatrix')
                )
                new_mat = local_reflexion_mat * orig_node_mat * world_reflexion_mat
                cmds.xform(new_node, matrix=new_mat, worldSpace=True)
                cmds.setAttr(new_node + '.scale', 1, 1, 1)
            if mirror_type.lower() == 'orientation':
                world_reflexion_mat = om2.MMatrix([
                    -1.0, -0.0, -0.0, 0.0,
                     0.0,  1.0,  0.0, 0.0,
                     0.0,  0.0,  1.0, 0.0,
                     0.0,  0.0,  0.0, 1.0
                ])
                orig_node_mat = om2.MMatrix(
                    cmds.getAttr(orig_node + '.worldMatrix')
                )
                new_mat = orig_node_mat * world_reflexion_mat
                cmds.xform(new_node, matrix=new_mat, worldSpace=True)
                cmds.setAttr(new_node + '.scale', 1, 1, 1)
                orig_orient = cmds.xform(orig_node, q=True, rotation=True, ws=True)
                cmds.xform(new_node, rotation=orig_orient, ws=True)
