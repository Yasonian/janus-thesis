// SPDX-License-Identifier: Apache-2.0
// Copyright Pionix GmbH and Contributors to EVerest
#include "v2g_key_controlImpl.hpp"

#include <everest/logging.hpp>

namespace module {
namespace v2g_key {

void v2g_key_controlImpl::init() {
}

void v2g_key_controlImpl::ready() {
    // Publish initial state so subscribers have a baseline immediately.
    publish_current_mode(current_mode);
    publish_cp_state(cp_state);
    publish_pp_present(pp_present);
    EVLOG_info << "VirtualV2GKey ready: mode=" << current_mode << " cp=" << cp_state;
}

// ---------------------------------------------------------------------------
// Command: set_power_path
// ---------------------------------------------------------------------------
bool v2g_key_controlImpl::handle_set_power_path(std::string& mode) {
    std::lock_guard<std::mutex> lock(state_mutex);

    if (current_step != TransitionStep::IDLE) {
        EVLOG_warning << "VirtualV2GKey: set_power_path(" << mode
                      << ") rejected — transition already in progress";
        return false;
    }
    if (mode == current_mode) {
        EVLOG_info << "VirtualV2GKey: set_power_path(" << mode << ") — already in that mode";
        return false;
    }
    if (mode != "AC" && mode != "DC_BPT") {
        EVLOG_error << "VirtualV2GKey: set_power_path: unknown mode '" << mode << "'";
        return false;
    }

    current_step = TransitionStep::STEP1_ENDING_HLC_SESSION;
    target_mode = mode;
    transition_start_time = std::chrono::steady_clock::now();

    // Publish switch_in_progress before spawning the thread.
    types::v2g_key::SwitchEvent start_evt;
    start_evt.from_mode = types::v2g_key::string_to_from_mode(current_mode);
    start_evt.to_mode = types::v2g_key::string_to_to_mode(target_mode);
    publish_switch_in_progress(start_evt);

    std::string from = current_mode;
    std::string to = mode;

    if (transition_thread.joinable()) {
        transition_thread.join();
    }
    transition_thread = std::thread([this, from, to]() { run_transition(from, to); });

    return true;
}

// ---------------------------------------------------------------------------
// Command: get_status
// ---------------------------------------------------------------------------
types::v2g_key::V2GKeyStatus v2g_key_controlImpl::handle_get_status() {
    std::lock_guard<std::mutex> lock(state_mutex);

    types::v2g_key::V2GKeyStatus status;
    status.current_mode = types::v2g_key::string_to_current_mode(current_mode);
    status.cp_state = types::v2g_key::string_to_cp_state(cp_state);
    status.pp_present = pp_present;
    status.transition_active = (current_step != TransitionStep::IDLE && current_step != TransitionStep::FAULT);

    if (status.transition_active) {
        switch (current_step) {
        case TransitionStep::STEP1_ENDING_HLC_SESSION:
            status.active_step = types::v2g_key::Active_step::STEP1_ENDING_HLC_SESSION;
            break;
        case TransitionStep::STEP2_CP_RETURNING_B1:
            status.active_step = types::v2g_key::Active_step::STEP2_CP_RETURNING_B1;
            break;
        case TransitionStep::STEP3_SWITCHING_SOURCE:
            status.active_step = types::v2g_key::Active_step::STEP3_SWITCHING_SOURCE;
            break;
        case TransitionStep::STEP4_ADVERTISING_NEW_SOURCE:
            status.active_step = types::v2g_key::Active_step::STEP4_ADVERTISING_NEW_SOURCE;
            break;
        default:
            break;
        }
    }

    return status;
}

// ---------------------------------------------------------------------------
// Internal: four-step transition sequence
// ---------------------------------------------------------------------------

void v2g_key_controlImpl::run_transition(std::string from_mode, std::string to_mode) {
    EVLOG_info << "VirtualV2GKey: transition " << from_mode << " -> " << to_mode << " started";

    step1_end_hlc_session();
    {
        std::lock_guard<std::mutex> lock(state_mutex);
        if (current_step == TransitionStep::FAULT) return;
        current_step = TransitionStep::STEP2_CP_RETURNING_B1;
    }

    step2_cp_return_b1();
    {
        std::lock_guard<std::mutex> lock(state_mutex);
        if (current_step == TransitionStep::FAULT) return;
        current_step = TransitionStep::STEP3_SWITCHING_SOURCE;
    }

    step3_switch_source();
    {
        std::lock_guard<std::mutex> lock(state_mutex);
        if (current_step == TransitionStep::FAULT) return;
        current_step = TransitionStep::STEP4_ADVERTISING_NEW_SOURCE;
    }

    step4_advertise_new_source(to_mode);

    auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - transition_start_time).count();

    EVLOG_info << "VirtualV2GKey: transition " << from_mode << " -> " << to_mode
               << " complete in " << elapsed_ms << " ms"
               << (elapsed_ms > 60000 ? " [OVER NFR-01 60 s budget]" : "");

    {
        std::lock_guard<std::mutex> lock(state_mutex);
        if (current_step == TransitionStep::FAULT) return;
        current_mode = to_mode;
        current_step = TransitionStep::IDLE;
    }

    publish_current_mode(to_mode);

    types::v2g_key::SwitchEvent done_evt;
    done_evt.from_mode = types::v2g_key::string_to_from_mode(from_mode);
    done_evt.to_mode = types::v2g_key::string_to_to_mode(to_mode);
    done_evt.elapsed_ms = static_cast<int>(elapsed_ms);
    done_evt.success = true;
    publish_switch_complete(done_evt);
}

// Step 1 — End ISO 15118 HLC session.
// Simulation: fixed delay stands in for the SessionStop exchange.
// TODO(JANUS): Replace with a subscription to the JANUS session_stopped
//              event once the JANUS module exists; remove the sleep.
void v2g_key_controlImpl::step1_end_hlc_session() {
    EVLOG_info << "VirtualV2GKey Step 1: ending HLC session (simulated delay)";
    std::this_thread::sleep_for(
        std::chrono::milliseconds(mod->config.hlc_teardown_simulation_ms));
}

// Step 2 — CP returns to IEC 61851 State B1 (connected, PWM off).
// PP remains asserted; the vehicle does not perceive an unplug.
void v2g_key_controlImpl::step2_cp_return_b1() {
    EVLOG_info << "VirtualV2GKey Step 2: CP returning to B1 (PWM off, PP present)";
    {
        std::lock_guard<std::mutex> lock(state_mutex);
        cp_state = "B1";
    }
    publish_cp_state("B1");
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
}

// Step 3 — Physical CP source switch (<100 ms configurable).
// During the switch the vehicle briefly observes State E (~0 V).
// IEC 61851-1 mandates vehicles tolerate this without a replug sequence.
void v2g_key_controlImpl::step3_switch_source() {
    EVLOG_info << "VirtualV2GKey Step 3: CP source switch — vehicle sees transient State E ("
               << mod->config.state_e_duration_ms << " ms)";
    {
        std::lock_guard<std::mutex> lock(state_mutex);
        cp_state = "E";
    }
    publish_cp_state("E");

    // Transient State E lasts for state_e_duration_ms (default 500 ms).
    std::this_thread::sleep_for(
        std::chrono::milliseconds(mod->config.state_e_duration_ms));

    // Physical relay closes within physical_switch_ms (default 100 ms).
    std::this_thread::sleep_for(
        std::chrono::milliseconds(mod->config.physical_switch_ms));
}

// Step 4 — New source advertises PWM; vehicle re-negotiates a fresh session.
// TODO(JANUS): After asserting B1 here, emit a signal to JANUS so it can
//              initiate the SDP / SupportedAppProtocol handshake for the
//              target protocol (ISO 15118-2 for AC or ISO 15118-20 for DC_BPT).
void v2g_key_controlImpl::step4_advertise_new_source(const std::string& new_mode) {
    EVLOG_info << "VirtualV2GKey Step 4: new source (" << new_mode
               << ") advertising PWM — vehicle will re-negotiate";
    {
        std::lock_guard<std::mutex> lock(state_mutex);
        cp_state = "B1";
    }
    publish_cp_state("B1");
    // Brief stabilisation period before the vehicle responds to the new PWM.
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
}

// ---------------------------------------------------------------------------
// Fault helper
// ---------------------------------------------------------------------------
void v2g_key_controlImpl::trigger_fault(const std::string& reason) {
    EVLOG_error << "VirtualV2GKey FAULT: " << reason;
    {
        std::lock_guard<std::mutex> lock(state_mutex);
        current_step = TransitionStep::FAULT;
        pp_present = false;
        fault_reason = reason;
    }
    publish_pp_present(false);
    publish_fault(reason);

    types::v2g_key::SwitchEvent fail_evt;
    fail_evt.from_mode = types::v2g_key::string_to_from_mode(current_mode);
    fail_evt.to_mode = types::v2g_key::string_to_to_mode(target_mode.empty() ? current_mode : target_mode);
    fail_evt.elapsed_ms = static_cast<int>(
        std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - transition_start_time).count());
    fail_evt.success = false;
    publish_switch_complete(fail_evt);
}

} // namespace v2g_key
} // namespace module
