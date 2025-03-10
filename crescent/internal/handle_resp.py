from __future__ import annotations

from asyncio import Future
from contextlib import suppress
from logging import getLogger
from typing import TYPE_CHECKING, NamedTuple

from hikari import (
    AutocompleteInteraction,
    AutocompleteInteractionOption,
    CommandInteraction,
    CommandType,
    InteractionType,
    Locale,
    OptionType,
    Snowflake,
)
from hikari.api import InteractionResponseBuilder
from hikari.impl import AutocompleteChoiceBuilder

from crescent.context import AutocompleteContext, Context
from crescent.internal.app_command import Unique
from crescent.mentionable import Mentionable
from crescent.utils import unwrap

if TYPE_CHECKING:
    from typing import Any, Sequence

    from hikari import CommandInteractionOption, Message, PartialInteraction, User

    from crescent.client import Client
    from crescent.internal import AppCommandMeta, Includable
    from crescent.typedefs import CommandHookCallbackT


_log = getLogger(__name__)

__all__: Sequence[str] = ("handle_resp",)


async def handle_resp(
    client: Client,
    interaction: PartialInteraction,
    future: Future[InteractionResponseBuilder] | None,
) -> None:
    if not isinstance(interaction, (CommandInteraction, AutocompleteInteraction)):
        return

    command_name, group, sub_group, _ = _get_crescent_command_data(interaction)

    command = _get_command(
        client, command_name, interaction.command_type, interaction.guild_id, group, sub_group
    )

    if not command:
        if not client.allow_unknown_interactions:
            _log.warning(
                f"Handler for command `{command_name}` does not exist locally. (If this is"
                " intended, add `allow_unknown_interactions=True` to the Client's constructor.)"
            )
        return

    ctx = _context_from_interaction_resp(client, interaction)
    ctx._rest_interaction_future = future

    if interaction.type is InteractionType.AUTOCOMPLETE:
        await _handle_autocomplete_resp(command, ctx.into(AutocompleteContext))
    else:
        await _handle_slash_resp(command, ctx.into(Context))


async def _handle_hooks(hooks: Sequence[CommandHookCallbackT], ctx: Context) -> bool:
    """Returns `False` if the command should not be run."""
    for hook in hooks:
        hook_res = await hook(ctx)

        if hook_res and hook_res.exit:
            return True
    return False


async def _handle_slash_resp(command: Includable[AppCommandMeta], ctx: Context) -> None:
    should_exit = await _handle_hooks(command.metadata.hooks, ctx)

    if should_exit:
        return

    try:
        await command.metadata.callback(ctx, **ctx.options)
        _ = await _handle_hooks(command.metadata.after_hooks, ctx)
    except Exception as exc:
        handled = await command.client._command_error_handler.try_handle(exc, [exc, ctx])
        await command.client.on_crescent_command_error(exc, ctx.into(Context), handled)


async def _handle_autocomplete_resp(
    command: Includable[AppCommandMeta], ctx: AutocompleteContext
) -> None:
    if not command.metadata.autocomplete:
        return

    option = _get_option_recursive(ctx.interaction.options)
    if not option:
        return
    autocomplete = command.metadata.autocomplete[option.name]

    try:
        res = await autocomplete(ctx, option)
        choices = [AutocompleteChoiceBuilder(name, value) for name, value in res]
        if future := ctx._unset_future:
            future.set_result(ctx.interaction.build_response(choices))
        else:
            await ctx.interaction.create_response(choices)
    except Exception as exc:
        handled = await command.client._autocomplete_error_handler.try_handle(
            exc, [exc, ctx, option]
        )
        await command.client.on_crescent_autocomplete_error(
            exc, ctx.into(AutocompleteContext), option, handled
        )


def _get_option_recursive(
    options: Sequence[AutocompleteInteractionOption],
) -> AutocompleteInteractionOption | None:
    for option in options:
        if option.is_focused:
            return option
        if not option.options:
            continue
        if maybe_option := _get_option_recursive(option.options):
            return maybe_option
    return None


def _get_command(
    client: Client,
    name: str,
    type: CommandType | int,
    guild_id: Snowflake | None,
    group: str | None,
    sub_group: str | None,
) -> Includable[AppCommandMeta] | None:
    kwargs: dict[str, Any] = dict(name=name, type=type, group=group, sub_group=sub_group)

    with suppress(KeyError):
        return client._command_handler._get(Unique(guild_id=guild_id, **kwargs))
    with suppress(KeyError):
        return client._command_handler._get(Unique(guild_id=None, **kwargs))
    return None


_VALUE_TYPE_LINK: dict[OptionType | int, Sequence[str]] = {
    OptionType.ROLE: ("roles",),
    OptionType.USER: ("members", "users"),
    OptionType.CHANNEL: ("channels",),
    OptionType.ATTACHMENT: ("attachments",),
}


class CrescentCommandData(NamedTuple):
    """Represents the information crescent needs to understand commands"""

    command_name: str
    group: str | None
    sub_group: str | None
    options: Sequence[CommandInteractionOption] | None


def _get_crescent_command_data(
    interaction: CommandInteraction | AutocompleteInteraction,
) -> CrescentCommandData:
    command_name: str = interaction.command_name
    group: str | None = None
    sub_group: str | None = None
    options = interaction.options

    if options:
        option = options[0]
        if option.type == 1:
            group = command_name
            command_name = option.name
            options = option.options
        elif option.type == 2:
            group = interaction.command_name
            sub_group = option.name
            command_name = unwrap(option.options)[0].name
            options = unwrap(option.options)[0].options

    return CrescentCommandData(
        command_name=command_name, group=group, sub_group=sub_group, options=options
    )


def _context_from_interaction_resp(
    client: Client, interaction: CommandInteraction | AutocompleteInteraction
) -> Context:
    command_name, group, sub_group, options = _get_crescent_command_data(interaction)

    if interaction.command_type is CommandType.SLASH:
        callback_options = _options_to_kwargs(interaction, options)
    else:
        # This will never be `AutocompleteInteraction` because message and user
        # commands don't have autocomplete.
        assert isinstance(interaction, CommandInteraction)
        callback_options = _resolved_data_to_kwargs(interaction)

    return Context(
        interaction=interaction,
        app=client.app,
        client=client,
        application_id=interaction.application_id,
        type=interaction.type,
        token=interaction.token,
        id=interaction.id,
        version=interaction.version,
        channel_id=interaction.channel_id,
        guild_id=interaction.guild_id,
        registered_guild_id=interaction.registered_guild_id,
        user=interaction.user,
        member=interaction.member,
        entitlements=interaction.entitlements,
        locale=Locale(interaction.locale),
        command=command_name,
        group=group,
        sub_group=sub_group,
        command_type=CommandType(interaction.command_type),
        options=callback_options,
        _has_created_response=False,
        _has_deferred_response=False,
        _rest_interaction_future=None,
    )


def _options_to_kwargs(
    interaction: CommandInteraction | AutocompleteInteraction,
    options: Sequence[CommandInteractionOption] | None,
) -> dict[str, Any]:
    if not options:
        return {}

    return {option.name: _extract_value(option, interaction) for option in options}


def _get_resolved(interaction: CommandInteraction, option_type: int) -> Any | None:
    attrs: Sequence[str] | None = _VALUE_TYPE_LINK.get(option_type)

    if attrs is None:
        return None

    for resolved_type in attrs:
        if data := getattr(interaction.resolved, resolved_type, None):
            return data

    return None


def _extract_value(
    option: CommandInteractionOption, interaction: CommandInteraction | AutocompleteInteraction
) -> Any:
    # `option.value` is guaranteed to have a value because this is not a command group.
    assert option.value is not None

    if isinstance(interaction, AutocompleteInteraction):
        return option.value

    if option.type is OptionType.MENTIONABLE:
        return Mentionable._from_interaction(interaction)

    resolved = _get_resolved(interaction, option.type)

    if resolved is None:
        return option.value

    return resolved[option.value]


def _resolved_data_to_kwargs(interaction: CommandInteraction) -> dict[str, Message | User]:
    if not interaction.resolved:
        raise ValueError("interaction.resolved should be defined when running this function")

    if interaction.resolved.messages:
        return {"message": next(iter(interaction.resolved.messages.values()))}
    if interaction.resolved.members:
        return {"user": next(iter(interaction.resolved.members.values()))}
    if interaction.resolved.users:
        return {"user": next(iter(interaction.resolved.users.values()))}

    raise AttributeError(
        "interaction.resolved did not have property `messages`, `members`, or `users`"
    )
