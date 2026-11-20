// SPDX-License-Identifier: Apache-2.0
// Copyright Pionix GmbH and Contributors to EVerest
#ifndef VIRTUAL_V2G_KEY_HPP
#define VIRTUAL_V2G_KEY_HPP

//
// AUTO GENERATED - MARKED REGIONS WILL BE KEPT
// template version 2
//

#include "ld-ev.hpp"

// headers for provided interface implementations
#include <generated/interfaces/v2g_key_control/Implementation.hpp>

// ev@4bf81b14-a215-475c-a1d3-0a484ae48918:v1
// insert your custom include headers here
// ev@4bf81b14-a215-475c-a1d3-0a484ae48918:v1

namespace module {

struct Conf {
    int physical_switch_ms;
    int state_e_duration_ms;
    int hlc_teardown_simulation_ms;
};

class VirtualV2GKey : public Everest::ModuleBase {
public:
    VirtualV2GKey() = delete;
    VirtualV2GKey(const ModuleInfo& info, std::unique_ptr<v2g_key_controlImplBase> p_v2g_key, Conf& config) :
        ModuleBase(info), p_v2g_key(std::move(p_v2g_key)), config(config){};

    const std::unique_ptr<v2g_key_controlImplBase> p_v2g_key;
    const Conf& config;

    // ev@1fce4c5e-0ab8-41bb-90f7-14277703d2ac:v1
    // insert your public definitions here
    // ev@1fce4c5e-0ab8-41bb-90f7-14277703d2ac:v1

protected:
    // ev@4714b2ab-a24f-4b95-ab81-36439e1478de:v1
    // insert your protected definitions here
    // ev@4714b2ab-a24f-4b95-ab81-36439e1478de:v1

private:
    friend class LdEverest;
    void init();
    void ready();

    // ev@211cfdbe-f69a-4cd6-a4ec-f8aaa3d1b6c8:v1
    // insert your private definitions here
    // ev@211cfdbe-f69a-4cd6-a4ec-f8aaa3d1b6c8:v1
};

// ev@087e516b-124c-48df-94fb-109508c7cda9:v1
// insert other definitions here
// ev@087e516b-124c-48df-94fb-109508c7cda9:v1

} // namespace module

#endif // VIRTUAL_V2G_KEY_HPP
