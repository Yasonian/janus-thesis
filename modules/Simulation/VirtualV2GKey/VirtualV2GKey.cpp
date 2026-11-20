// SPDX-License-Identifier: Apache-2.0
// Copyright Pionix GmbH and Contributors to EVerest
#include "VirtualV2GKey.hpp"
#include "v2g_key/v2g_key_controlImpl.hpp"

namespace module {

void VirtualV2GKey::init() {
    invoke_init(*p_v2g_key);
}

void VirtualV2GKey::ready() {
    invoke_ready(*p_v2g_key);
}

} // namespace module
