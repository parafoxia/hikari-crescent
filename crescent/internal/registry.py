from __future__ import annotations
from asyncio import gather
from itertools import chain

from typing import TYPE_CHECKING, Dict, cast
from weakref import WeakValueDictionary

from hikari import UNDEFINED, CommandOption, OptionType, ShardReadyEvent, Snowflake

from crescent.utils import gather_iter
from crescent.utils.options import unwrap
from crescent.internal.app_command import AppCommand, AppCommandType
from crescent.internal.meta_struct import MetaStruct
from crescent.internal.app_command import AppCommandMeta
from crescent.internal.app_command import Unique

if TYPE_CHECKING:
    from typing import Callable, Any, Awaitable, Optional, Sequence
    from hikari import Command, UndefinedOr
    from crescent.bot import Bot


def register_command(
    callback: Callable[..., Awaitable[Any]],
    guild: Optional[Snowflake] = None,
    group: Optional[str] = None,
    sub_group: Optional[str] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
    options: Optional[Sequence[CommandOption]] = None,
    default_permission: UndefinedOr[bool] = UNDEFINED
):

    name = name or callback.__name__
    description = description or "\u200B"

    meta: MetaStruct[AppCommandMeta] = MetaStruct(
        callback=callback,
        manager=None,
        metadata=AppCommandMeta(
            group=group,
            sub_group=sub_group,
            app=AppCommand(
                type=AppCommandType.CHAT_INPUT,
                description=description,
                guild_id=guild,
                name=name,
                options=options,
                default_permission=default_permission
            )
        )
    )

    return meta


class CommandHandler:

    __slots__: Sequence[str] = (
        "registry",
        "bot",
        "guilds",
        "application_id",
    )

    def __init__(self, bot: Bot, guilds: Sequence[Snowflake]) -> None:
        self.bot: Bot = bot
        self.guilds: Sequence[Snowflake] = guilds
        self.application_id: Optional[Snowflake] = None

        self.registry: WeakValueDictionary[
            Unique,
            MetaStruct[AppCommandMeta]
        ] = WeakValueDictionary()

    def register(self, command: MetaStruct[AppCommandMeta]) -> MetaStruct[AppCommandMeta]:
        command.metadata.app.guild_id = command.metadata.app.guild_id or self.bot.default_guild
        self.registry[command.metadata.unique] = command
        return command

    async def get_discord_commands(self) -> Sequence[AppCommand]:
        """Fetches commands from Discord"""

        commands = list(
            await self.bot.rest.fetch_application_commands(unwrap(self.application_id))
        )

        commands.extend(
            *await gather_iter(
                self.bot.rest.fetch_application_commands(
                    unwrap(self.application_id), guild=guild
                )
                for guild in self.guilds
            )
        )

        def hikari_to_crescent_command(command: Command) -> AppCommand:
            return AppCommand(
                type=AppCommandType.CHAT_INPUT,
                name=command.name,
                description=command.description,
                guild_id=command.guild_id,
                options=command.options,
                default_permission=command.default_permission,
                id=command.id,
            )

        return [
            hikari_to_crescent_command(command)
            for command in commands
        ]

    def build_commands(self) -> Sequence[AppCommand]:

        built_commands: Dict[Unique, AppCommand] = {}

        for command in self.registry.values():
            command.metadata.app.guild_id = (
                command.metadata.app.guild_id or self.bot.default_guild
            )

            if command.metadata.sub_group:
                # If a command has a sub_group, it must be nested 2 levels deep.
                #
                # command
                #     subcommand-group
                #         subcommand
                #
                # The children of the subcommand-group object are being set to include
                # `command` If that subcommand-group object does not exist, it will be
                # created here. The same goes for the top-level command.
                #
                # First make sure the command exists. This command will hold the
                # subcommand-group for `command`.

                # `key` represents the unique value for the top-level command that will
                # hold the subcommand.
                key = Unique(
                    name=unwrap(command.metadata.group),
                    type=command.metadata.app.type,
                    guild_id=command.metadata.app.guild_id,
                    group=None,
                    sub_group=None,
                )

                if key not in built_commands:
                    built_commands[key] = AppCommand(
                        name=unwrap(command.metadata.group),
                        description="HIDDEN",
                        type=AppCommandType.CHAT_INPUT,
                        guild_id=command.metadata.app.guild_id,
                        options=[],
                        default_permission=command.metadata.app.default_permission
                    )

                # The top-level command now exists. A subcommand group now if placed
                # inside the top-level command. This subcommand group will hold `command`.

                children = unwrap(built_commands[key].options)

                sub_command_group = CommandOption(
                    name=command.metadata.sub_group,
                    description="HIDDEN",
                    type=OptionType.SUB_COMMAND_GROUP,
                    options=[],
                    is_required=None,  # type: ignore
                )

                # This for-else makes sure that sub_command_group will hold a reference
                # to the subcommand group that we want to modify to hold `command`
                for cmd_in_children in children:
                    if all(
                        (
                            cmd_in_children.name == sub_command_group.name,
                            cmd_in_children.description == sub_command_group.description,
                            cmd_in_children.type == sub_command_group.type
                        )
                    ):
                        sub_command_group = cmd_in_children
                        break
                else:
                    cast(list, children).append(sub_command_group)

                cast(list, sub_command_group.options).append(CommandOption(
                    name=command.metadata.app.name,
                    description=command.metadata.app.description,
                    type=OptionType.SUB_COMMAND,
                    options=command.metadata.app.options,
                    is_required=None,  # type: ignore
                ))

                continue

            if command.metadata.group:
                # Any command at this point will only have one level of nesting.
                #
                # Command
                #    subcommand
                #
                # A subcommand object is what is being generated here. If there is no
                # top level command, it will be created here.

                # `key` represents the unique value for the top-level command that will
                # hold the subcommand.
                key = Unique(
                    name=command.metadata.group,
                    type=command.metadata.app.type,
                    guild_id=command.metadata.app.guild_id,
                    group=None,
                    sub_group=None,
                )

                if key not in built_commands:
                    built_commands[key] = AppCommand(
                        name=command.metadata.group,
                        description="HIDDEN",
                        type=command.metadata.app.type,
                        guild_id=command.metadata.app.guild_id,
                        options=[],
                        default_permission=command.metadata.app.default_permission
                    )

                # No checking has to be done before appending `command` since it is the
                # lowest level.
                cast(list, built_commands[key].options).append(
                    CommandOption(
                        name=command.metadata.app.name,
                        description=command.metadata.app.description,
                        type=command.metadata.app.type,
                        options=command.metadata.app.options,
                        is_required=None,  # type: ignore
                    )
                )

                continue

            built_commands[Unique.from_meta_struct(command)] = command.metadata.app

        return tuple(built_commands.values())

    async def create_application_command(self, command: AppCommand):
        await self.bot.rest.create_application_command(
            application=unwrap(self.application_id),
            name=command.name,
            description=command.description,
            guild=command.guild_id or UNDEFINED,
            options=command.options or UNDEFINED,
            default_permission=command.default_permission
        )

    async def delete_application_command(self, command: AppCommand):
        await self.bot.rest.delete_application_command(
            application=unwrap(self.application_id),
            command=unwrap(command.id),
            guild=command.guild_id or UNDEFINED
        )

    async def init(self, event: ShardReadyEvent):
        self.application_id = event.application_id
        self.guilds = self.guilds or tuple(self.bot.cache.get_guilds_view().keys())

        discord_commands = await self.get_discord_commands()
        local_commands = self.build_commands()

        to_delete = filter(
            lambda dc: not any(dc.is_same_command(lc) for lc in local_commands),
            discord_commands
        )
        to_post = list(filter(lambda lc: lc not in discord_commands, local_commands))

        await gather(*chain(
            map(self.delete_application_command, to_delete),
            map(self.create_application_command, to_post),
        ))
