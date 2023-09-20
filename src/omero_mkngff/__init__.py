#!/usr/bin/env python

#
# Copyright (c) 2023 German BioImaging.
# All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import os
from argparse import Namespace
from pathlib import Path
from typing import Generator, Tuple

import omero.all  # noqa
from omero.cli import BaseControl, Parser
from omero.sys import ParametersI

SUFFIX = "mkngff"
HELP = """Plugin to swap OMERO filesets with NGFF

CLI plugin used to swap an existing OMERO fileset with

Examples:

    # Generate SQL needed for initial setup
    omero mkngff setup

    # Generate SQL for converting the given fileset
    omero mkngff sql ${fileset} ${zarrdir}

    # ... while overriding the name of the directory under the ManagedRepository
    omero mkngff sql ${fileset} ${zarrdir} --zarr_name "nice.ome.zarr"

"""

SETUP = """

CREATE OR REPLACE FUNCTION mkngff_fileset(
    old_fileset bigint,
    uuid character varying,
    repo character varying,
    prefix character varying,
    info text[][])
  RETURNS integer AS
$BODY$
DECLARE
   new_event integer;
   new_fileset integer;
   new_file integer;
   new_ann integer;
   old_owner integer;
   old_group integer;
   old_perms integer;

BEGIN

    select _current_or_new_event() into new_event;

    select
        owner_id, group_id, permissions
     into
        old_owner, old_group, old_perms
     from fileset where id = old_fileset;

    insert into fileset
        (id, templateprefix, {DETAILS1})
        values
        (nextval('seq_fileset'), prefix, {DETAILS2})
        returning id into new_fileset;

    insert into annotation
        (id, {DETAILS1}, ns, longvalue, discriminator)
        values
        (nextval('seq_annotation'), {DETAILS2},
        'mkngff', old_fileset, '/basic/num/long/')
        returning id into new_ann;

    insert into filesetannotationlink
        (id, {DETAILS1}, parent, child)
        values
        (nextval('seq_filesetannotationlink'), {DETAILS2}, new_fileset, new_ann);

    for i in 1 .. array_upper(info, 1)
    loop

      insert into originalfile
          (id, {DETAILS1}, mimetype, repo, path, name)
          values (nextval('seq_originalfile'), {DETAILS2},
            info[i][3], repo, info[i][1], uuid || info[i][2])
          returning id into new_file;

      insert into filesetentry
          (id, {DETAILS1}, fileset, originalfile, fileset_index, clientpath)
          values (nextval('seq_filesetentry'), {DETAILS2},
                  new_fileset, new_file, i-1, 'unknown');

    end loop;

    update image set fileset = new_fileset where fileset = old_fileset;

    RETURN new_fileset;
END;
$BODY$
  LANGUAGE plpgsql VOLATILE;

""".format(
    DETAILS1="permissions, creation_id, group_id, owner_id, update_id",
    DETAILS2="old_perms, new_event, old_group, old_owner, new_event",
)

TEMPLATE = """
begin;
    select mkngff_fileset(
      {OLD_FILESET},
      '{UUID}',
      '{REPO}',
      '{PREFIX}',
      array[
{ROWS}
      ]::text[][]
    );
commit;
"""

ROW = """          ['{PATH}', '{NAME}', '{MIME}']"""


class MkngffControl(BaseControl):
    def _configure(self, parser: Parser) -> None:
        parser.add_login_arguments()
        sub = parser.add_subparsers()

        setup = sub.add_parser("setup", help="print SQL setup statement")
        setup.set_defaults(func=self.setup)

        sql = sub.add_parser("sql", help="generate SQL statement")
        sql.add_argument(
            "--secret", help="DB UUID for protecting SQL statements", default="TBD"
        )
        sql.add_argument("--zarr_name", help="Nicer name for zarr directory if desired")
        sql.add_argument(
            "--symlink_repo",
            help=("Create symlinks from Fileset to symlink_target using"
                  "this ManagedRepo path, e.g. /data/OMERO/ManagedRepository")
        )
        sql.add_argument("fileset_id", type=int)
        sql.add_argument("symlink_target")
        sql.set_defaults(func=self.sql)

        # symlink command to ONLY create symlinks - useful if you have previously generated
        # the corresponding sql for a Fileset
        symlink = sub.add_parser("symlink", help="Create managed repo symlink")
        symlink.add_argument("symlink_repo", help=(
            "Create symlinks from Fileset to symlink_target using"
            "this ManagedRepo path, e.g. /data/OMERO/ManagedRepository"))
        symlink.add_argument("fileset_id", type=int)
        symlink.add_argument("symlink_target")
        symlink.set_defaults(func=self.symlink)

    def setup(self, args: Namespace) -> None:
        self.ctx.out(SETUP)

    def sql(self, args: Namespace) -> None:
        prefix = self.get_prefix(args)

        prefix_path, prefix_name = prefix.rsplit("/", 1)
        self.ctx.err(
            f"Found prefix {prefix_path} // {prefix_name} for fileset {args.fileset_id}"
        )

        symlink_path = Path(args.symlink_target)

        if not symlink_path.exists():
            self.ctx.die(401, f"Symlink target does not exist: {args.symlink_target}")
            return

        # create *_SUFFIX/path/to/zarr directory containing symlink to data
        if args.symlink_repo:
            self.create_symlink(args.symlink_repo, prefix, symlink_path, args.symlink_target)

        rows = []
        # Need a file to set path/name on pixels table BioFormats uses for setId()
        setid_target = None
        for row_path, row_name, row_mime in self.walk(symlink_path):
            # remove common path to shorten
            row_path = str(row_path).replace(f"{symlink_path.parent}", "")
            if str(row_path).startswith("/"):
                row_path = str(row_path)[1:]  # remove "/" from start
            row_full_path = f"{prefix_path}/{prefix_name}_{SUFFIX}/{row_path}"
            # pick the first .zattrs file we find, then update to ome.xml if we find it
            if setid_target is None and row_name == ".zattrs" or row_name == "METADATA.ome.xml":
                setid_target = [row_full_path, row_name]
            rows.append(
                ROW.format(
                    PATH=f"{row_full_path}/",
                    NAME=row_name,
                    MIME=row_mime,
                )
            )

        # Add a command to update the Pixels table with path/name using old Fileset ID *before* new Fileset is created
        fpath = setid_target[0]
        fname = setid_target[1]
        self.ctx.out(f"UPDATE pixels SET name = '{fname}', path = '{fpath}' where image in (select id from Image where fileset = {args.fileset_id});")

        self.ctx.out(
            TEMPLATE.format(
                OLD_FILESET=args.fileset_id,
                PREFIX=f"{prefix_path}/{prefix_name}_{SUFFIX}/",
                ROWS=",\n".join(rows),
                REPO=self.get_uuid(args),
                UUID=args.secret,
            )
        )

    def symlink(self, args: Namespace) -> None:
        prefix = self.get_prefix(args)
        symlink_path = Path(args.symlink_target)
        self.create_symlink(args.symlink_repo, prefix, symlink_path, args.symlink_target)

    def get_prefix(self, args):

        conn = self.ctx.conn(args)  # noqa
        q = conn.sf.getQueryService()
        rv = q.findAllByQuery(
            (
                "select f from Fileset f join fetch f.usedFiles fe "
                "join fetch fe.originalFile ofile where f.id = :id"
            ),
            ParametersI().addId(args.fileset_id),
        )
        if len(rv) != 1:
            self.ctx.die(400, f"Found wrong number of filesets: {len(rv)}")
            return

        prefix = rv[0].templatePrefix.val

        if prefix.endswith("/"):
            prefix = prefix[:-1]  # Drop ending "/"

        return prefix

    def create_symlink(self, symlink_repo, prefix, symlink_path, symlink_target):

        prefix_dir = os.path.join(symlink_repo, prefix)
        self.ctx.err(f"Checking for prefix_dir {prefix_dir}")
        if not os.path.exists(prefix_dir):
                self.ctx.die(402, f"Fileset dir does not exist: {prefix_dir}")
        symlink_container = f"{symlink_path.parent}"
        if symlink_container.startswith("/"):
            symlink_container = symlink_container[1:]  # remove "/" from start
        symlink_dir = f"{prefix_dir}_{SUFFIX}"
        self.ctx.err(f"Creating dir at {symlink_dir}")
        os.makedirs(symlink_dir, exist_ok=True)

        symlink_source = os.path.join(symlink_dir, symlink_path.name)
        target_is_directory = os.path.isdir(symlink_target)
        self.ctx.err(
            f"Creating symlink {symlink_source} -> {symlink_target}"
        )
        os.symlink(symlink_target, symlink_source, target_is_directory)

    def walk(self, path: Path) -> Generator[Tuple[Path, str, str], None, None]:
        for p in path.iterdir():
            if not p.is_dir():
                yield (p.parent, p.name, "application/octet-stream")
            else:
                is_array = (p / ".zarray").exists()
                if is_array or (p / ".zgroup").exists():
                    yield (p.parent, p.name, "Directory")
                    # Don't try to walk zarray - will only contain chunks!
                    if not is_array:
                        yield from self.walk(p)
                else:
                    # Chunk directory
                    continue

    def get_uuid(self, args: Namespace) -> str:
        from omero.grid import ManagedRepositoryPrx as MRepo

        client = self.ctx.conn(args)
        shared = client.sf.sharedResources()
        repos = shared.repositories()
        repos = list(zip(repos.descriptions, repos.proxies))

        for idx, pair in enumerate(repos):
            desc, prx = pair
            is_mrepo = MRepo.checkedCast(prx)
            if is_mrepo:
                return desc.hash.val

        raise self.ctx.die(
            402, f"Failed to find managed repository (count={len(repos)})"
        )
