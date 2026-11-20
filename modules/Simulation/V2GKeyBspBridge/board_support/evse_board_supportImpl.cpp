// SPDX-License-Identifier: Apache-2.0
// Copyright Pionix GmbH and Contributors to EVerest

#include "evse_board_supportImpl.hpp"

namespace module {
namespace board_support {

std::optional<types::board_support_common::Event>
evse_board_supportImpl::cp_state_to_bsp_event(const std::string& cp_state) {
    using types::board_support_common::Event;
    // VirtualV2GKey cp_state enum: A, B1, B2, C, E (see interfaces/v2g_key_control.yaml).
    // BspEvent enum: A,B,C,D,E,F,... (see types/board_support_common.yaml).
    if (cp_state == "A") {
        return Event::A;
    } else if (cp_state == "B1" || cp_state == "B2") {
        return Event::B; // BspEvent has no B1/B2 split; PWM is carried by pwm_on/off cmds
    } else if (cp_state == "C") {
        return Event::C;
    } else if (cp_state == "E") {
        return Event::E;
    }
    return std::nullopt;
}

void evse_board_supportImpl::init() {
    // ---- CP-state event: passthrough by default, suppressed while overriding ----
    mod->r_bsp_downstream->subscribe_event([this](types::board_support_common::BspEvent event) {
        {
            std::lock_guard<std::mutex> lock(last_event_mutex);
            last_downstream_event = event.event;
            has_last_downstream_event = true;
        }
        if (!override_active.load()) {
            publish_event(event); // transparent passthrough
        } else {
            EVLOG_debug << "V2GKeyBspBridge: suppressing downstream CP event during switch override";
        }
    });

    // ---- All other BSP vars: transparent passthrough (Yeti -> manager) ----
    mod->r_bsp_downstream->subscribe_capabilities(
        [this](types::evse_board_support::HardwareCapabilities c) { publish_capabilities(c); });
    mod->r_bsp_downstream->subscribe_ac_nr_of_phases_available([this](int n) { publish_ac_nr_of_phases_available(n); });
    mod->r_bsp_downstream->subscribe_ac_pp_ampacity(
        [this](types::board_support_common::ProximityPilot p) { publish_ac_pp_ampacity(p); });
    mod->r_bsp_downstream->subscribe_telemetry(
        [this](types::evse_board_support::Telemetry t) { publish_telemetry(t); });
    mod->r_bsp_downstream->subscribe_request_stop_transaction(
        [this](types::evse_manager::StopTransactionRequest r) { publish_request_stop_transaction(r); });

    // ---- VirtualV2GKey control: drive the override state machine ----
    mod->r_v2g_key->subscribe_switch_in_progress([this](types::v2g_key::SwitchEvent) {
        override_active.store(true);
        EVLOG_info << "V2GKeyBspBridge: switch_in_progress -> ENTER override (suppressing Yeti CP feed)";
    });

    mod->r_v2g_key->subscribe_switch_complete([this](types::v2g_key::SwitchEvent) {
        override_active.store(false);
        EVLOG_info << "V2GKeyBspBridge: switch_complete -> EXIT override (resuming Yeti CP feed)";
        // Resync the manager to the real downstream CP state on the way out.
        std::lock_guard<std::mutex> lock(last_event_mutex);
        if (has_last_downstream_event) {
            types::board_support_common::BspEvent resync;
            resync.event = last_downstream_event;
            publish_event(resync);
        }
    });

    mod->r_v2g_key->subscribe_cp_state([this](std::string cp_state) {
        if (!override_active.load()) {
            return; // outside a switch, VirtualV2GKey cp_state is informational only
        }
        auto mapped = cp_state_to_bsp_event(cp_state);
        if (mapped.has_value()) {
            types::board_support_common::BspEvent injected;
            injected.event = mapped.value();
            publish_event(injected);
            EVLOG_info << "V2GKeyBspBridge: injected CP event from v2g_key cp_state=" << cp_state;
        }
    });
}

void evse_board_supportImpl::ready() {
}

// ---- Command handlers: forward 1:1 to the downstream (Yeti) board support ----
void evse_board_supportImpl::handle_enable(bool& value) {
    mod->r_bsp_downstream->call_enable(value);
}

void evse_board_supportImpl::handle_pwm_on(double& value) {
    mod->r_bsp_downstream->call_pwm_on(value);
}

void evse_board_supportImpl::handle_pwm_off() {
    mod->r_bsp_downstream->call_pwm_off();
}

void evse_board_supportImpl::handle_pwm_F() {
    mod->r_bsp_downstream->call_pwm_F();
}

void evse_board_supportImpl::handle_allow_power_on(types::evse_board_support::PowerOnOff& value) {
    mod->r_bsp_downstream->call_allow_power_on(value);
}

void evse_board_supportImpl::handle_ac_switch_three_phases_while_charging(bool& value) {
    mod->r_bsp_downstream->call_ac_switch_three_phases_while_charging(value);
}

void evse_board_supportImpl::handle_evse_replug(int& value) {
    mod->r_bsp_downstream->call_evse_replug(value);
}

void evse_board_supportImpl::handle_ac_set_overcurrent_limit_A(double& value) {
    mod->r_bsp_downstream->call_ac_set_overcurrent_limit_A(value);
}

} // namespace board_support
} // namespace module
