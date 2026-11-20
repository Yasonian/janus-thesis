// SPDX-License-Identifier: Apache-2.0
// Copyright Pionix GmbH and Contributors to EVerest
#include "V2GKeyBspBridge.hpp"

namespace module {

void V2GKeyBspBridge::init() {
    invoke_init(*p_board_support);
}

void V2GKeyBspBridge::ready() {
    invoke_ready(*p_board_support);
}

} // namespace module
