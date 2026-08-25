 west build -b nrf7120dk/nrf7120/cpuapp --sysbuild \
	-d build_nrf7120_system_off zephyr/samples/boards/nordic/system_off \
	-- \
	-Dsystem_off_CONFIG_APP_USE_RETAINED_MEM=n \
	-Dsystem_off_CONFIG_GRTC_WAKEUP_ENABLE=n \
	-Dsystem_off_CONFIG_GPIO_WAKEUP_ENABLE=n \
	-Dsystem_off_CONFIG_SYS_CLOCK_DISABLE=y \
	-Dsystem_off_CONFIG_FLASH_LOAD_OFFSET=0xA000
