/*
 * Copyright (c) 2024, Nordic Semiconductor ASA
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <zephyr/devicetree.h>
#include <zephyr/kernel.h>
#include <zephyr/drivers/retained_mem/nrf_retained_mem.h>

#include <helpers/nrfx_ram_ctrl.h>

#if defined(CONFIG_BUILD_WITH_TFM)
/* Non-secure build: MEMCONF is owned by the secure domain, so route retention
 * requests to the secure RAM-control service instead of touching nrfx directly.
 */
#include <tfm/tfm_ioctl_api.h>
#endif

#define _BUILD_MEM_REGION(node_id)		    \
	{.dt_addr = DT_REG_ADDR(DT_PARENT(node_id)),\
	 .dt_size = DT_REG_SIZE(DT_PARENT(node_id))},

struct ret_mem_region {
	uintptr_t dt_addr;
	size_t dt_size;
};

static const struct ret_mem_region ret_mem_regions[] = {
	DT_FOREACH_STATUS_OKAY(zephyr_retained_ram, _BUILD_MEM_REGION)
};

int z_nrf_retained_mem_retention_apply(void)
{
	const struct ret_mem_region *rmr;

	for (size_t i = 0; i < ARRAY_SIZE(ret_mem_regions); i++) {
		rmr = &ret_mem_regions[i];
#if defined(CONFIG_BUILD_WITH_TFM)
		(void)nrf_ram_ctrl_svc_retention_set(rmr->dt_addr, rmr->dt_size, true);
#else
		nrfx_ram_ctrl_retention_enable_set((void *)rmr->dt_addr, rmr->dt_size, true);
#endif
	}

	return 0;
}
