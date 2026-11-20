// SPDX-License-Identifier: Apache-2.0
// Copyright Pionix GmbH and Contributors to EVerest
#ifndef V2G_KEY_V2G_KEY_CONTROL_IMPL_HPP
#define V2G_KEY_V2G_KEY_CONTROL_IMPL_HPP

//
// AUTO GENERATED - MARKED REGIONS WILL BE KEPT
// template version 3
//

#include <generated/interfaces/v2g_key_control/Implementation.hpp>

#include "../VirtualV2GKey.hpp"

// ev@75ac1216-19eb-4182-a85c-820f1fc2c091:v1
// insert your custom include headers here
#include <atomic>
#include <chrono>
#include <mutex>
#include <thread>
// ev@75ac1216-19eb-4182-a85c-820f1fc2c091:v1

namespace module {
namespace v2g_key {

struct Conf {};

class v2g_key_controlImpl : public v2g_key_controlImplBase {
public:
    v2g_key_controlImpl() = delete;
    v2g_key_controlImpl(Everest::ModuleAdapter* ev, const Everest::PtrContainer<VirtualV2GKey>& mod, Conf& config) :
        v2g_key_controlImplBase(ev, "v2g_key"), mod(mod), config(config){};

    // ev@8ea32d28-373f-4c90-ae5e-b4fcc74e2a61:v1
    // insert your public definitions here
    // ev@8ea32d28-373f-4c90-ae5e-b4fcc74e2a61:v1

protected:
    // command handler functions (virtual)
    virtual bool handle_set_power_path(std::string& mode) override;
    virtual types::v2g_key::V2GKeyStatus handle_get_status() override;

    // ev@d2d1847a-7b88-41dd-ad07-92785f06f5c4:v1
    // insert your protected definitions here
    // ev@d2d1847a-7b88-41dd-ad07-92785f06f5c4:v1

private:
    const Everest::PtrContainer<VirtualV2GKey>& mod;
    const Conf& config;

    virtual void init() override;
    virtual void ready() override;

    // ev@3370e4dd-95f4-47a9-aaec-ea76f34a66c9:v1
    // insert your private definitions here

    // --- Four-step transition state machine ---
    // Step labels follow the vendor-confirmed P-ONE V2G-Key transition sequence:
    //   Step 1: End ISO 15118 HLC session with vehicle
    //   Step 2: CP returns to IEC 61851 State B1 (connected, PWM off)
    //   Step 3: CP source switched (<100 ms); vehicle briefly sees State E (~0 V)
    //   Step 4: New source advertises PWM; vehicle re-negotiates fresh session
    enum class TransitionStep : uint8_t {
        IDLE,
        STEP1_ENDING_HLC_SESSION,
        STEP2_CP_RETURNING_B1,
        STEP3_SWITCHING_SOURCE,   // encompasses the transient State E sub-phase
        STEP4_ADVERTISING_NEW_SOURCE,
        FAULT,
    };

    std::mutex state_mutex;
    TransitionStep current_step{TransitionStep::IDLE};
    std::string current_mode{"AC"};   // initial power path is AC
    std::string cp_state{"B1"};       // connected, no PWM (idle between sessions)
    bool pp_present{true};            // PP always present; false only on fault
    std::string target_mode{};
    std::string fault_reason{};

    std::thread transition_thread;
    std::chrono::steady_clock::time_point transition_start_time;

    // Runs on transition_thread; executes the four steps in sequence.
    void run_transition(std::string from_mode, std::string to_mode);

    void trigger_fault(const std::string& reason);

    // TODO(JANUS): Step 1 will be replaced by a JANUS-driven SessionStop
    //              handshake; remove the simulated delay and subscribe to
    //              the JANUS session_stopped event instead.
    void step1_end_hlc_session();

    void step2_cp_return_b1();
    void step3_switch_source();
    void step4_advertise_new_source(const std::string& new_mode);
    // ev@3370e4dd-95f4-47a9-aaec-ea76f34a66c9:v1
};

// ev@3d7da0ad-02c2-493d-9920-0bbbd56b9876:v1
// insert other definitions here
// ev@3d7da0ad-02c2-493d-9920-0bbbd56b9876:v1

} // namespace v2g_key
} // namespace module

#endif // V2G_KEY_V2G_KEY_CONTROL_IMPL_HPP
