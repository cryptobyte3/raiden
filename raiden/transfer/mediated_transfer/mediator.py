import itertools
import random
from typing import List, Dict

from raiden.constants import MAXIMUM_PENDING_TRANSFERS
from raiden.transfer import channel, secret_registry
from raiden.transfer.architecture import TransitionResult
from raiden.transfer.events import (
    ContractSendChannelBatchUnlock,
    SendProcessed,
)
from raiden.transfer.mediated_transfer.events import (
    CHANNEL_IDENTIFIER_GLOBAL_QUEUE,
    EventUnlockFailed,
    EventUnlockSuccess,
    EventUnlockClaimFailed,
    EventUnlockClaimSuccess,
    SendRevealSecret,
)
from raiden.transfer.mediated_transfer.state import (
    LockedTransferSignedState,
    LockedTransferUnsignedState,
    MediationPairState,
    MediatorTransferState,
)
from raiden.transfer.mediated_transfer.state_change import (
    ActionInitMediator,
    ReceiveLockExpired,
    ReceiveSecretReveal,
    ReceiveTransferRefund,
)
from raiden.transfer.state import (
    NettingChannelState,
    message_identifier_from_prng,
    CHANNEL_STATE_OPENED,
)
from raiden.transfer.state_change import (
    Block,
    ContractReceiveSecretReveal,
    ReceiveUnlock,
)
from raiden.settings import DEFAULT_NUMBER_OF_CONFIRMATIONS_BLOCK
from raiden.utils import typing


STATE_SECRET_KNOWN = (
    'payee_secret_revealed',
    'payee_contract_unlock',
    'payee_balance_proof',

    'payer_secret_revealed',
    'payer_waiting_close',
    'payer_waiting_unlock',
    'payer_balance_proof',
)
STATE_TRANSFER_PAID = (
    'payee_contract_unlock',
    'payee_balance_proof',

    'payer_balance_proof',
)
# TODO: fix expired state, it is not final
STATE_TRANSFER_FINAL = (
    'payee_contract_unlock',
    'payee_balance_proof',
    'payee_expired',

    'payer_balance_proof',
    'payer_expired',
)


def is_lock_valid(expiration, block_number):
    """ True if the lock has not expired. """
    return block_number <= expiration


def is_safe_to_wait(lock_expiration, reveal_timeout, block_number):
    """ True if there are more than enough blocks to safely unlock on chain,
    making waiting is safe.
    """
    # reveal timeout will not ever be larger than the lock_expiration otherwise
    # the expected block_number is negative
    assert block_number > 0
    assert reveal_timeout > 0
    assert lock_expiration > reveal_timeout

    lock_timeout = lock_expiration - block_number

    # A node may wait for a new balance proof while there are reveal_timeout
    # blocks left, at that block and onwards it is not safe to wait.
    if lock_timeout > reveal_timeout:
        return True, None

    msg = (
        f'lock timeout is unsafe.'
        f' timeout must be larger than {reveal_timeout}, but it is {lock_timeout}.'
        f' expiration: {lock_expiration} block_number: {block_number}'
    )
    return False, msg


def is_channel_usable(candidate_channel_state, transfer_amount, lock_timeout):
    pending_transfers = channel.get_number_of_pending_transfers(candidate_channel_state.our_state)
    distributable = channel.get_distributable(
        candidate_channel_state.our_state,
        candidate_channel_state.partner_state,
    )

    return (
        lock_timeout > 0 and
        channel.get_status(candidate_channel_state) == CHANNEL_STATE_OPENED and
        candidate_channel_state.settle_timeout >= lock_timeout and
        candidate_channel_state.reveal_timeout < lock_timeout and
        pending_transfers < MAXIMUM_PENDING_TRANSFERS and
        transfer_amount <= distributable and
        channel.is_valid_amount(candidate_channel_state.our_state, transfer_amount)
    )


def is_send_transfer_almost_equal(
        send: LockedTransferUnsignedState,
        received: LockedTransferSignedState,
):
    """ True if both transfers are for the same mediated transfer. """
    # The only thing that may change is the direction of the transfer
    return (
        isinstance(send, LockedTransferUnsignedState) and
        isinstance(received, LockedTransferSignedState) and
        send.payment_identifier == received.payment_identifier and
        send.token == received.token and
        send.lock.amount == received.lock.amount and
        send.lock.expiration == received.lock.expiration and
        send.lock.secrethash == received.lock.secrethash and
        send.initiator == received.initiator and
        send.target == received.target
    )


def filter_used_routes(transfers_pair, routes):
    """This function makes sure we filter routes that have already been used.

    So in a setup like this, we want to make sure that node 2, having tried to
    route the transfer through 3 will also try 5 before sending it backwards to 1

    1 -> 2 -> 3 -> 4
         v         ^
         5 -> 6 -> 7
    """
    channelid_to_route = {r.channel_identifier: r for r in routes}

    for pair in transfers_pair:
        channelid = pair.payer_transfer.balance_proof.channel_identifier
        if channelid in channelid_to_route:
            del channelid_to_route[channelid]

        channelid = pair.payee_transfer.balance_proof.channel_identifier
        if channelid in channelid_to_route:
            del channelid_to_route[channelid]

    return list(channelid_to_route.values())


def get_payee_channel(channelidentifiers_to_channels, transfer_pair):
    """ Returns the payee channel of a given transfer pair. """
    payee_channel_identifier = transfer_pair.payee_transfer.balance_proof.channel_identifier
    assert payee_channel_identifier in channelidentifiers_to_channels
    payee_channel = channelidentifiers_to_channels[payee_channel_identifier]

    return payee_channel


def get_payer_channel(channelidentifiers_to_channels, transfer_pair):
    """ Returns the payer channel of a given transfer pair. """
    payer_channel_identifier = transfer_pair.payer_transfer.balance_proof.channel_identifier
    assert payer_channel_identifier in channelidentifiers_to_channels
    payer_channel = channelidentifiers_to_channels[payer_channel_identifier]

    return payer_channel


def get_pending_transfer_pairs(transfers_pair):
    """ Return the transfer pairs that are not at a final state. """
    pending_pairs = list(
        pair
        for pair in transfers_pair
        if pair.payee_state not in STATE_TRANSFER_FINAL or
        pair.payer_state not in STATE_TRANSFER_FINAL
    )
    return pending_pairs


def sanity_check(state):
    """ Check invariants that must hold. """

    # if a transfer is paid we must know the secret
    all_transfers_states = itertools.chain(
        (pair.payee_state for pair in state.transfers_pair),
        (pair.payer_state for pair in state.transfers_pair),
    )
    if any(state in STATE_TRANSFER_PAID for state in all_transfers_states):
        assert state.secret is not None

    # the "transitivity" for these values is checked below as part of
    # almost_equal check
    if state.transfers_pair:
        first_pair = state.transfers_pair[0]
        assert state.secrethash == first_pair.payer_transfer.lock.secrethash

    for pair in state.transfers_pair:
        assert is_send_transfer_almost_equal(pair.payee_transfer, pair.payer_transfer)
        assert pair.payer_state in pair.valid_payer_states
        assert pair.payee_state in pair.valid_payee_states

    for original, refund in zip(state.transfers_pair[:-1], state.transfers_pair[1:]):
        assert is_send_transfer_almost_equal(original.payee_transfer, refund.payer_transfer)
        assert original.payee_address == refund.payer_address
        assert original.payee_transfer.lock.expiration == refund.payer_transfer.lock.expiration


def clear_if_finalized(iteration):
    """ Clear the state if all transfer pairs have finalized. """
    state = iteration.new_state

    if state is None:
        return iteration

    # TODO: clear the expired transfer, this will need some sort of
    # synchronization among the nodes
    all_finalized = all(
        pair.payee_state in STATE_TRANSFER_PAID and pair.payer_state in STATE_TRANSFER_PAID
        for pair in state.transfers_pair
    )

    if all_finalized:
        return TransitionResult(None, iteration.events)
    return iteration


def next_channel_from_routes(
        available_routes: List['RouteState'],
        channelidentifiers_to_channels: Dict,
        transfer_amount: int,
        lock_timeout: int,
) -> NettingChannelState:
    """ Returns the first route that may be used to mediated the transfer.
    The routing service can race with local changes, so the recommended routes
    must be validated.
    Args:
        available_routes: Current available routes that may be used, it's
            assumed that the available_routes list is ordered from best to
            worst.
        channelidentifiers_to_channels: Mapping from channel identifier
            to NettingChannelState.
        transfer_amount: The amount of tokens that will be transferred
            through the given route.
        lock_timeout: Number of blocks until the lock expires, used to filter
            out channels that have a smaller settlement window.
    Returns:
        The next route.
    """
    for route in available_routes:
        channel_state = channelidentifiers_to_channels.get(route.channel_identifier)

        if not channel_state:
            continue

        if is_channel_usable(channel_state, transfer_amount, lock_timeout):
            return channel_state

    return None


def next_transfer_pair(
        payer_transfer: LockedTransferSignedState,
        available_routes: List['RouteState'],
        channelidentifiers_to_channels: Dict,
        pseudo_random_generator: random.Random,
        block_number: typing.BlockNumber,
):
    """ Given a payer transfer tries a new route to proceed with the mediation.
    Args:
        payer_transfer: The transfer received from the payer_channel.
        available_routes: Current available routes that may be used, it's
            assumed that the routes list is ordered from best to worst.
        channelidentifiers_to_channels: All the channels available for this
            transfer.
        pseudo_random_generator: Number generator to generate a message id.
        block_number: The current block number.
    """
    transfer_pair = None
    mediated_events = list()
    lock_timeout = payer_transfer.lock.expiration - block_number

    payee_channel = next_channel_from_routes(
        available_routes,
        channelidentifiers_to_channels,
        payer_transfer.lock.amount,
        lock_timeout,
    )

    if payee_channel:
        assert payee_channel.settle_timeout >= lock_timeout
        assert payee_channel.token_address == payer_transfer.token

        message_identifier = message_identifier_from_prng(pseudo_random_generator)
        lockedtransfer_event = channel.send_lockedtransfer(
            payee_channel,
            payer_transfer.initiator,
            payer_transfer.target,
            payer_transfer.lock.amount,
            message_identifier,
            payer_transfer.payment_identifier,
            payer_transfer.lock.expiration,
            payer_transfer.lock.secrethash,
        )
        assert lockedtransfer_event

        transfer_pair = MediationPairState(
            payer_transfer,
            payee_channel.partner_state.address,
            lockedtransfer_event.transfer,
        )

        mediated_events = [lockedtransfer_event]

    return (
        transfer_pair,
        mediated_events,
    )


def set_secret(state, channelidentifiers_to_channels, secret, secrethash):
    """ Set the secret to all mediated transfers.
    It doesn't matter if the secret was learned through the blockchain or a
    secret reveal message.
    """
    state.secret = secret

    for pair in state.transfers_pair:
        payer_channel = channelidentifiers_to_channels[
            pair.payer_transfer.balance_proof.channel_identifier
        ]
        channel.register_secret(
            payer_channel,
            secret,
            secrethash,
        )

        payee_channel = channelidentifiers_to_channels[
            pair.payee_transfer.balance_proof.channel_identifier
        ]
        channel.register_secret(
            payee_channel,
            secret,
            secrethash,
        )


def set_onchain_secret(state, channelidentifiers_to_channels, secret, secrethash):
    """ Set the secret to all mediated transfers.

    The secret should have been learned from the secret registry.
    """
    state.secret = secret

    for pair in state.transfers_pair:
        payer_channel = channelidentifiers_to_channels[
            pair.payer_transfer.balance_proof.channel_identifier
        ]
        channel.register_onchain_secret(
            payer_channel,
            secret,
            secrethash,
        )

        payee_channel = channelidentifiers_to_channels[
            pair.payee_transfer.balance_proof.channel_identifier
        ]
        channel.register_onchain_secret(
            payee_channel,
            secret,
            secrethash,
        )


def set_payee_state_and_check_reveal_order(  # pylint: disable=invalid-name
        transfers_pair,
        payee_address,
        new_payee_state,
):
    """ Set the state of a transfer *sent* to a payee and check the secret is
    being revealed backwards.
    Note:
        The elements or transfers_pair are changed in place, the list must
        contain all the known transfers to properly check reveal order.
    """
    assert new_payee_state in MediationPairState.valid_payee_states

    wrong_reveal_order = False
    for back in reversed(transfers_pair):
        if back.payee_address == payee_address:
            back.payee_state = new_payee_state
            break

        elif back.payee_state not in STATE_SECRET_KNOWN:
            wrong_reveal_order = True

    if wrong_reveal_order:
        # TODO: Append an event for byzantine behavior.
        # XXX: This can happen if a mediator in the middle of the chain of hops
        # learns the secret faster than its subsequent nodes. Should a byzantine
        # behavior notification be added or should we fix the events_for_unlock function?
        return list()

    return list()


def set_expired_pairs(transfers_pair, block_number):
    """ Set the state transfers to the expired state and return the failed events."""
    pending_transfers_pairs = get_pending_transfer_pairs(transfers_pair)

    events = list()
    for pair in pending_transfers_pairs:
        has_payee_transfer_expired = (
            block_number > pair.payee_transfer.lock.expiration and
            pair.payee_state != 'payee_expired'
        )
        has_payer_transfer_expired = (
            block_number > pair.payer_transfer.lock.expiration and
            pair.payer_state != 'payer_expired'
        )

        if has_payer_transfer_expired:
            # For safety, the correct behavior is:
            #
            # - If the payee has been paid, then the payer must pay too.
            #
            #   And the corollary:
            #
            # - If the payer transfer has expired, then the payee transfer must
            #   have expired too.
            #
            # The problem is that this corollary cannot be asserted. If a user
            # is running Raiden without a monitoring service, then it may go
            # offline after having paid a transfer to a payee, but without
            # getting a balance proof of the payer, and once it comes back
            # online the transfer may have expired.
            #
            # assert pair.payee_state == 'payee_expired'

            pair.payer_state = 'payer_expired'
            unlock_failed = EventUnlockClaimFailed(
                pair.payer_transfer.payment_identifier,
                pair.payer_transfer.lock.secrethash,
                'lock expired',
            )
            events.append(unlock_failed)

        if has_payee_transfer_expired:
            pair.payee_state = 'payee_expired'
            unlock_failed = EventUnlockFailed(
                pair.payee_transfer.payment_identifier,
                pair.payee_transfer.lock.secrethash,
                'lock expired',
            )
            events.append(unlock_failed)

    return events


def events_for_refund_transfer(
        refund_channel,
        transfer_to_refund,
        pseudo_random_generator,
        block_number,
):
    """ Refund the transfer.
    Args:
        refund_route (RouteState): The original route that sent the mediated
            transfer to this node.
        transfer_to_refund (LockedTransferSignedState): The original mediated transfer
            from the refund_route.
        timeout_blocks (int): The number of blocks available from the /latest
            transfer/ received by this node, this transfer might be the
            original mediated transfer (if no route was available) or a refund
            transfer from a down stream node.
        block_number (int): The current block number.
    Returns:
        An empty list if there are not enough blocks to safely create a refund,
        or a list with a refund event."""
    lock_timeout = transfer_to_refund.lock.expiration - block_number
    transfer_amount = transfer_to_refund.lock.amount

    if is_channel_usable(refund_channel, transfer_amount, lock_timeout):
        message_identifier = message_identifier_from_prng(pseudo_random_generator)
        refund_transfer = channel.send_refundtransfer(
            refund_channel,
            transfer_to_refund.initiator,
            transfer_to_refund.target,
            transfer_to_refund.lock.amount,
            message_identifier,
            transfer_to_refund.payment_identifier,
            transfer_to_refund.lock.expiration,
            transfer_to_refund.lock.secrethash,
        )

        return [refund_transfer]

    # Can not create a refund lock with a safe expiration, so don't do anything
    # and wait for the received lock to expire.
    return list()


def events_for_revealsecret(transfers_pair, secret, pseudo_random_generator):
    """ Reveal the secret backwards.

    This node is named N, suppose there is a mediated transfer with two refund
    transfers, one from B and one from C:

        A-N-B...B-N-C..C-N-D

    Under normal operation N will first learn the secret from D, then reveal to
    C, wait for C to inform the secret is known before revealing it to B, and
    again wait for B before revealing the secret to A.

    If B somehow sent a reveal secret before C and D, then the secret will be
    revealed to A, but not C and D, meaning the secret won't be propagated
    forward. Even if D sent a reveal secret at about the same time, the secret
    will only be revealed to B upon confirmation from C.

    If the proof doesn't arrive in time and the lock's expiration is at risk, N
    won't lose tokens since it knows the secret can go on-chain at any time.
    """
    events = list()
    for pair in reversed(transfers_pair):
        payee_secret = pair.payee_state in STATE_SECRET_KNOWN
        payer_secret = pair.payer_state in STATE_SECRET_KNOWN
        should_send_secret = pair.payer_state == 'payer_pending'

        if payee_secret and not payer_secret and should_send_secret:
            message_identifier = message_identifier_from_prng(pseudo_random_generator)
            pair.payer_state = 'payer_secret_revealed'
            payer_transfer = pair.payer_transfer
            revealsecret = SendRevealSecret(
                recipient=payer_transfer.balance_proof.sender,
                channel_identifier=CHANNEL_IDENTIFIER_GLOBAL_QUEUE,
                message_identifier=message_identifier,
                secret=secret,
            )

            events.append(revealsecret)

    return events


def events_for_balanceproof(
        channelidentifiers_to_channels,
        transfers_pair,
        pseudo_random_generator,
        block_number,
        secret,
        secrethash,
):
    """ While it's safe do the off-chain unlock. """

    events = list()
    for pair in reversed(transfers_pair):
        payee_knows_secret = pair.payee_state in STATE_SECRET_KNOWN
        payee_payed = pair.payee_state in STATE_TRANSFER_PAID

        payee_channel = get_payee_channel(channelidentifiers_to_channels, pair)
        payee_channel_open = channel.get_status(payee_channel) == CHANNEL_STATE_OPENED

        payer_channel = get_payer_channel(channelidentifiers_to_channels, pair)

        # The mediator must not send to the payee a balance proof if the lock
        # is in the danger zone, because the payer may not do the same and the
        # on-chain unlock may fail. If the lock is nearing it's expiration
        # block, then on-chain unlock should be done, and if successfull it can
        # be unlocked off-chain.
        is_safe_to_send_balanceproof, _ = is_safe_to_wait(
            pair.payer_transfer.lock.expiration,
            payer_channel.reveal_timeout,
            block_number,
        )

        should_send_balanceproof_to_payee = (
            payee_channel_open and
            payee_knows_secret and
            not payee_payed and
            is_safe_to_send_balanceproof
        )

        if should_send_balanceproof_to_payee:
            pair.payee_state = 'payee_balance_proof'

            message_identifier = message_identifier_from_prng(pseudo_random_generator)
            unlock_lock = channel.send_unlock(
                payee_channel,
                pair.payee_transfer.payment_identifier,
                message_identifier,
                secret,
                secrethash,
            )

            unlock_success = EventUnlockSuccess(
                pair.payer_transfer.payment_identifier,
                pair.payer_transfer.lock.secrethash,
            )
            events.append(unlock_lock)
            events.append(unlock_success)

    return events


def events_for_onchain_secretreveal(
        channelidentifiers_to_channels,
        transfers_pair,
        block_number,
):
    """ Reveal the secret if a locks is in the unsafe region. """
    events = list()
    pending_transfers_pairs = get_pending_transfer_pairs(transfers_pair)

    for pair in reversed(pending_transfers_pairs):
        payer_channel = get_payer_channel(channelidentifiers_to_channels, pair)
        expiration = pair.payer_transfer.lock.expiration

        safe_to_wait, _ = is_safe_to_wait(
            expiration,
            payer_channel.reveal_timeout,
            block_number,
        )

        # We check if the secret is already known by receiving a ReceiveSecretReveal state change
        secret_known = channel.is_secret_known(
            payer_channel.partner_state,
            pair.payer_transfer.lock.secrethash,
        )

        if not safe_to_wait and secret_known:
            secret = channel.get_secret(
                payer_channel.partner_state,
                pair.payer_transfer.lock.secrethash,
            )
            reveal_events = secret_registry.events_for_onchain_secretreveal(
                payer_channel,
                secret,
                expiration,
            )
            # short circuit because the secret needs to be revealed on-chain
            # only once
            return reveal_events

    return events


def events_for_unlock_if_closed(
        channelidentifiers_to_channels,
        transfers_pair,
        secret,
        secrethash,
):
    """ Unlock on chain if the payer channel is closed and the secret is known.
    If a channel is closed because of another task a balance proof will not be
    received, so there is no reason to wait for the unsafe region before
    calling close.
    This may break the reverse reveal order:
        Path: A -- B -- C -- B -- D
        B learned the secret from D and has revealed to C.
        C has not confirmed yet.
        channel(A, B).closed is True.
        B will unlock on channel(A, B) before C's confirmation.
        A may learn the secret faster than other nodes.
    """
    events = list()
    pending_transfers_pairs = get_pending_transfer_pairs(transfers_pair)

    for pair in pending_transfers_pairs:
        payer_channel = get_payer_channel(channelidentifiers_to_channels, pair)

        payer_channel_open = channel.get_status(payer_channel) == CHANNEL_STATE_OPENED

        # The unlock is done by the channel
        if not payer_channel_open:
            pair.payer_state = 'payer_waiting_unlock'

            partner_state = payer_channel.partner_state
            lock = channel.get_lock(partner_state, secrethash)
            unlock_proof = channel.compute_proof_for_lock(
                partner_state,
                secret,
                lock,
            )
            unlock = ContractSendChannelBatchUnlock(
                payer_channel.token_network_identifier,
                payer_channel.identifier,
                [unlock_proof],
            )
            events.append(unlock)

    return events


def secret_learned(
        state,
        channelidentifiers_to_channels,
        pseudo_random_generator,
        block_number,
        secret,
        secrethash,
        payee_address,
        new_payee_state,
        from_onchain_secretreveal,
):
    """ Set the state of the `payee_address` transfer, check the secret is
    being revealed backwards, and if necessary send out RevealSecret,
    SendBalanceProof, and Unlocks.
    """
    assert new_payee_state in STATE_SECRET_KNOWN

    # TODO: if any of the transfers is in expired state, event for byzantine behavior

    if state.secret is None:
        if from_onchain_secretreveal:
            set_onchain_secret(
                state,
                channelidentifiers_to_channels,
                secret,
                secrethash,
            )
        else:
            set_secret(
                state,
                channelidentifiers_to_channels,
                secret,
                secrethash,
            )

        # This task only needs to unlock if the channel is closed when the
        # secret is learned, otherwise the channel task will do it
        # automatically
        unlock = events_for_unlock_if_closed(
            channelidentifiers_to_channels,
            state.transfers_pair,
            secret,
            secrethash,
        )
    else:
        unlock = []

    wrong_order = set_payee_state_and_check_reveal_order(
        state.transfers_pair,
        payee_address,
        new_payee_state,
    )

    secret_reveal = events_for_revealsecret(
        state.transfers_pair,
        secret,
        pseudo_random_generator,
    )

    balance_proof = events_for_balanceproof(
        channelidentifiers_to_channels,
        state.transfers_pair,
        pseudo_random_generator,
        block_number,
        secret,
        secrethash,
    )

    iteration = TransitionResult(
        state,
        wrong_order + secret_reveal + balance_proof + unlock,
    )

    return iteration


def mediate_transfer(
        state,
        possible_routes,
        payer_channel,
        channelidentifiers_to_channels,
        pseudo_random_generator,
        payer_transfer,
        block_number,
):
    """ Try a new route or fail back to a refund.

    The mediator can safely try a new route knowing that the tokens from
    payer_transfer will cover the expenses of the mediation. If there is no
    route available that may be used at the moment of the call the mediator may
    send a refund back to the payer, allowing the payer to try a different
    route.
    """
    transfer_pair = None
    mediated_events = list()

    available_routes = filter_used_routes(
        state.transfers_pair,
        possible_routes,
    )

    assert payer_channel.partner_state.address == payer_transfer.balance_proof.sender

    transfer_pair, mediated_events = next_transfer_pair(
        payer_transfer,
        available_routes,
        channelidentifiers_to_channels,
        pseudo_random_generator,
        block_number,
    )

    # If none of the available_routes could be used, try a refund
    if transfer_pair is None:
        if state.transfers_pair:
            original_pair = state.transfers_pair[0]
            original_transfer = original_pair.payer_transfer
            original_channel = get_payer_channel(
                channelidentifiers_to_channels,
                original_pair,
            )
        else:
            original_channel = payer_channel
            original_transfer = payer_transfer

        refund_events = events_for_refund_transfer(
            original_channel,
            original_transfer,
            pseudo_random_generator,
            block_number,
        )

        iteration = TransitionResult(state, refund_events)

    else:
        # the list must be ordered from high to low expiration, expiration
        # handling depends on it
        state.transfers_pair.append(transfer_pair)
        iteration = TransitionResult(state, mediated_events)

    return iteration


def handle_init(
        state_change: MediatorTransferState,
        channelidentifiers_to_channels: typing.ChannelMap,
        pseudo_random_generator: random.Random,
        block_number: typing.BlockNumber,
):
    routes = state_change.routes

    from_route = state_change.from_route
    from_transfer = state_change.from_transfer
    payer_channel = channelidentifiers_to_channels.get(from_route.channel_identifier)

    # There is no corresponding channel for the message, ignore it
    if not payer_channel:
        return TransitionResult(None, [])

    mediator_state = MediatorTransferState(from_transfer.lock.secrethash)

    is_valid, events, _ = channel.handle_receive_lockedtransfer(
        payer_channel,
        from_transfer,
    )
    if not is_valid:
        return TransitionResult(None, events)

    iteration = mediate_transfer(
        mediator_state,
        routes,
        payer_channel,
        channelidentifiers_to_channels,
        pseudo_random_generator,
        from_transfer,
        block_number,
    )

    events.extend(iteration.events)
    return TransitionResult(iteration.new_state, events)


def handle_block(
        mediator_state: MediatorTransferState,
        state_change: Block,
        channelidentifiers_to_channels: typing.ChannelMap,
        pseudo_random_generator: random.Random,
        block_number: typing.BlockNumber,
):
    """ After Raiden learns about a new block this function must be called to
    handle expiration of the hash time locks.
    Args:
        state (MediatorTransferState): The current state.
    Return:
        TransitionResult: The resulting iteration
    """
    balance_proof = mediator_state.transfers_pair[0].payee_transfer.balance_proof
    channel_identifier = balance_proof.channel_identifier
    channel_state = channelidentifiers_to_channels.get(channel_identifier)

    secrethash = mediator_state.secrethash
    if secrethash in channel_state.our_state.secrethashes_to_lockedlocks:
        locked_lock = channel_state.our_state.secrethashes_to_lockedlocks[secrethash]
        lock_expiration_threshold = locked_lock.expiration + DEFAULT_NUMBER_OF_CONFIRMATIONS_BLOCK
        if state_change.block_number > lock_expiration_threshold:
            # Lock has expired, cleanup...
            channel.delete_secrethash_endstate(channel_state.our_state, secrethash)
            expired_lock_event = channel.events_for_expired_lock(
                channel_state,
                locked_lock,
                pseudo_random_generator,
            )

            iteration = TransitionResult(None, [expired_lock_event])
            return iteration

    secret_reveal_events = events_for_onchain_secretreveal(
        channelidentifiers_to_channels,
        mediator_state.transfers_pair,
        block_number,
    )

    unlock_fail_events = set_expired_pairs(
        mediator_state.transfers_pair,
        block_number,
    )

    iteration = TransitionResult(
        mediator_state,
        unlock_fail_events + secret_reveal_events,
    )

    return iteration


def handle_refundtransfer(
        mediator_state: MediatorTransferState,
        mediator_state_change: ReceiveTransferRefund,
        channelidentifiers_to_channels: typing.ChannelMap,
        pseudo_random_generator: random.Random,
        block_number: typing.BlockNumber,
):
    """ Validate and handle a ReceiveTransferRefund mediator_state change.
    A node might participate in mediated transfer more than once because of
    refund transfers, eg. A-B-C-B-D-T, B tried to mediate the transfer through
    C, which didn't have an available route to proceed and refunds B, at this
    point B is part of the path again and will try a new partner to proceed
    with the mediation through D, D finally reaches the target T.
    In the above scenario B has two pairs of payer and payee transfers:
        payer:A payee:C from the first SendLockedTransfer
        payer:C payee:D from the following SendRefundTransfer
    Args:
        mediator_state (MediatorTransferState): Current mediator_state.
        mediator_state_change (ReceiveTransferRefund): The mediator_state change.
    Returns:
        TransitionResult: The resulting iteration.
    """
    if mediator_state.secret is None:
        # The last sent transfer is the only one that may be refunded, all the
        # previous ones are refunded already.
        transfer_pair = mediator_state.transfers_pair[-1]
        payee_transfer = transfer_pair.payee_transfer
        payer_transfer = mediator_state_change.transfer
        channel_identifier = payer_transfer.balance_proof.channel_identifier
        payer_channel = channelidentifiers_to_channels[channel_identifier]
        is_valid, channel_events, _ = channel.handle_refundtransfer(
            received_transfer=payee_transfer,
            channel_state=payer_channel,
            refund=mediator_state_change,
        )
        if not is_valid:
            return TransitionResult(None, channel_events)

        iteration = mediate_transfer(
            mediator_state,
            mediator_state_change.routes,
            payer_channel,
            channelidentifiers_to_channels,
            pseudo_random_generator,
            payer_transfer,
            block_number,
        )

        events = list()
        events.extend(channel_events)
        events.extend(iteration.events)
    else:
        events = list()

    iteration = TransitionResult(mediator_state, events)
    return iteration


def handle_secretreveal(
        mediator_state,
        mediator_state_change,
        channelidentifiers_to_channels,
        pseudo_random_generator,
        block_number,
):
    """ Validate and handle a ReceiveSecretReveal mediator_state change.
    The Secret must propagate backwards through the chain of mediators, this
    function will record the learned secret, check if the secret is propagating
    backwards (for the known paths), and send the SendBalanceProof/RevealSecret if
    necessary.
    """
    is_secret_unknown = mediator_state.secret is None
    is_valid_reveal = mediator_state_change.secrethash == mediator_state.secrethash

    if is_secret_unknown and is_valid_reveal:
        iteration = secret_learned(
            mediator_state,
            channelidentifiers_to_channels,
            pseudo_random_generator,
            block_number,
            mediator_state_change.secret,
            mediator_state_change.secrethash,
            mediator_state_change.sender,
            'payee_secret_revealed',
            isinstance(mediator_state_change, ContractReceiveSecretReveal),
        )

    else:
        iteration = TransitionResult(mediator_state, list())

    return iteration


def handle_unlock(mediator_state, state_change: ReceiveUnlock, channelidentifiers_to_channels):
    """ Handle a ReceiveUnlock state change. """
    events = list()
    balance_proof_sender = state_change.balance_proof.sender
    channel_identifier = state_change.balance_proof.channel_identifier

    for pair in mediator_state.transfers_pair:
        if pair.payer_transfer.balance_proof.sender == balance_proof_sender:
            channel_state = channelidentifiers_to_channels.get(channel_identifier)

            if channel_state:
                is_valid, channel_events, _ = channel.handle_unlock(
                    channel_state,
                    state_change,
                )
                events.extend(channel_events)

                if is_valid:
                    unlock = EventUnlockClaimSuccess(
                        pair.payee_transfer.payment_identifier,
                        pair.payee_transfer.lock.secrethash,
                    )
                    events.append(unlock)

                    send_processed = SendProcessed(
                        recipient=balance_proof_sender,
                        channel_identifier=CHANNEL_IDENTIFIER_GLOBAL_QUEUE,
                        message_identifier=state_change.message_identifier,
                    )
                    events.append(send_processed)

                    pair.payer_state = 'payer_balance_proof'

    iteration = TransitionResult(mediator_state, events)
    return iteration


def handle_lock_expired(
        mediator_state: MediatorTransferState,
        state_change: ReceiveLockExpired,
        channelidentifiers_to_channels: typing.ChannelMap,
):
    from_route = state_change.from_route
    channel_state = channelidentifiers_to_channels.get(from_route.channel_identifier)

    result = channel.handle_receive_lock_expired(channel_state, state_change)

    return TransitionResult(
        None,
        result.events,
    )


def state_transition(
        mediator_state,
        state_change,
        channelidentifiers_to_channels,
        pseudo_random_generator,
        block_number,
):
    """ State machine for a node mediating a transfer. """
    # pylint: disable=too-many-branches
    # Notes:
    # - A user cannot cancel a mediated transfer after it was initiated, she
    #   may only reject to mediate before hand. This is because the mediator
    #   doesn't control the secret reveal and needs to wait for the lock
    #   expiration before safely discarding the transfer.

    iteration = TransitionResult(mediator_state, list())

    if isinstance(state_change, ActionInitMediator):
        if mediator_state is None:
            iteration = handle_init(
                state_change,
                channelidentifiers_to_channels,
                pseudo_random_generator,
                block_number,
            )

    elif isinstance(state_change, Block):
        iteration = handle_block(
            mediator_state,
            state_change,
            channelidentifiers_to_channels,
            pseudo_random_generator,
            block_number,
        )

    elif isinstance(state_change, ReceiveTransferRefund):
        iteration = handle_refundtransfer(
            mediator_state,
            state_change,
            channelidentifiers_to_channels,
            pseudo_random_generator,
            block_number,
        )

    elif isinstance(state_change, ReceiveSecretReveal):
        iteration = handle_secretreveal(
            mediator_state,
            state_change,
            channelidentifiers_to_channels,
            pseudo_random_generator,
            block_number,
        )

    elif isinstance(state_change, ContractReceiveSecretReveal):
        iteration = handle_secretreveal(
            mediator_state,
            state_change,
            channelidentifiers_to_channels,
            pseudo_random_generator,
            block_number,
        )

    elif isinstance(state_change, ReceiveUnlock):
        iteration = handle_unlock(
            mediator_state,
            state_change,
            channelidentifiers_to_channels,
        )

    elif isinstance(state_change, ReceiveLockExpired):
        iteration = handle_lock_expired(
            mediator_state,
            state_change,
            channelidentifiers_to_channels,
        )

    # this is the place for paranoia
    if iteration.new_state is not None:
        sanity_check(iteration.new_state)

    return clear_if_finalized(iteration)
