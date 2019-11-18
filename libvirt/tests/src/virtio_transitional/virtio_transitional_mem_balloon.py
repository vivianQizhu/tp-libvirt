import os
import re
import logging

from avocado.utils import download

from virttest import virsh
from virttest import data_dir
from virttest import utils_misc
from virttest.libvirt_xml import vm_xml
from virttest.utils_test import libvirt


def run(test, params, env):
    """
    Test virtio/virtio-transitional/virtio-non-transitional model of
    memory balloon

    :param test: Test object
    :param params: Dictionary with the test parameters
    :param env: Dictionary with test environment
    """

    def get_win_mon_free_mem(session):
        """
        Get Performance Monitored Free memory.

        :param session: shell Object
        :return string: freespace M-bytes
        """
        cmd = r'typeperf "\Memory\Free & Zero Page List Bytes" -sc 1'
        status, output = session.cmd_status_output(cmd)
        if status == 0:
            free = "%s" % re.findall(r"\d+\.\d+", output)[2]
            free = float(utils_misc.normalize_data_size(free, order_magnitude="M"))
            return int(free)
        else:
            test.fail("Failed to get windows guest free memory")


    def get_disk_vol(session):
        """
        Get virtio-win disk volume letter for windows guest.

        :param session: VM session.
        """
        key = "VolumeName like 'virtio-win%'"
        try:
            return utils_misc.get_win_disk_vol(session,
                                               condition=key)
        except Exception:
            test.error("Could not get virtio-win disk vol!")

    def operate_balloon_service(session, operation):
        """
        Run/check/stop/install/uninstall balloon service in windows guest

        :param session: shell Object
        :param operation: operation against balloon serive, e.g. run/status/
                          uninstall/stop
        :return: cmd execution output
        """
        logging.info("%s Balloon Service in guest." % operation)
        drive_letter = get_disk_vol(session)
        try:
            operate_cmd = params["%s_balloon_service"
                                 % operation] % drive_letter
            if operation == "status":
                output = session.cmd_output(operate_cmd)
            else:
                output = session.cmd(operate_cmd)
        except Exception as err:
            test.error("%s balloon service failed! Error msg is:\n%s"
                       % (operation, err))
        return output

    def configure_balloon_service(session):
        """
        Check balloon service and install it if it's not running.

        :param session: shell Object
        :param operation: operation against balloon serive, e.g. run/status/
                          uninstall/stop
        """
        logging.info("Check Balloon Service status before install service")
        output = operate_balloon_service(session, "status")
        if re.search("running", output.lower(), re.M):
            logging.info("Balloon service is already running !")
        elif re.search("stop", output.lower(), re.M):
            logging.info("Balloon service is stopped,start it now")
            operate_balloon_service(session, "run")
        else:
            logging.info("Install Balloon Service in guest.")
            operate_balloon_service(session, "install")

    vm_name = params.get("main_vm", "avocado-vt-vm1")
    vm = env.get_vm(params["main_vm"])
    vmxml = vm_xml.VMXML.new_from_inactive_dumpxml(vm_name)
    backup_xml = vmxml.copy()
    guest_src_url = params.get("guest_src_url")
    virtio_model = params['virtio_model']

    # Download and replace image when guest_src_url provided
    if guest_src_url:
        image_name = params['image_path']
        target_path = utils_misc.get_path(data_dir.get_data_dir(), image_name)
        if not os.path.exists(target_path):
            download.get_file(guest_src_url, target_path)
        params["blk_source_name"] = target_path

    try:
        # Update disk and interface to correct model
        if (params["os_variant"] == 'rhel6' or
                'rhel6' in params.get("shortname")):
            iface_params = {'model': 'virtio-transitional'}
            libvirt.modify_vm_iface(vm_name, "update_iface", iface_params)
        libvirt.set_vm_disk(vm, params)
        # vmxml will not be updated since set_vm_disk
        # sync with another dumped xml inside the function
        vmxml = vm_xml.VMXML.new_from_inactive_dumpxml(vm_name)
        # Update memory balloon device to correct model
        membal_dict = {'membal_model': virtio_model,
                       'membal_stats_period': '10'}
        libvirt.update_memballoon_xml(vmxml, membal_dict)
        if not vm.is_alive():
            vm.start()
        is_windows_guest = (params['os_type'] == 'Windows')
        session = vm.wait_for_login()
        # Check if memory balloon device exists on guest
        if not is_windows_guest:
            status = session.cmd_status_output('lspci |grep balloon')[0]
            if status != 0:
                test.fail('Not detect memory balloon device on guest.')
        else:
            configure_balloon_service(session)
        # Save and restore guest
        sn_path = os.path.join(data_dir.get_tmp_dir(), params['os_variant'])
        session.close()
        virsh.save(vm_name, sn_path)
        virsh.restore(sn_path)
        session = vm.wait_for_login()
        # Get original memory for later balloon function check
        ori_outside_mem = vm.get_max_mem()
        ori_guest_mem = vm.get_current_memory_size()
        used_mem = utils_misc.get_used_mem(session, params['os_type'])
        # balloon half of the memory
        ballooned_mem = ori_outside_mem // 2
        # Set memory to test balloon function
        virsh.setmem(vm_name, ballooned_mem)
        # Check if memory is ballooned successfully
        logging.info("Check memory status")
        unusable_mem = ori_outside_mem - ori_guest_mem
        gcompare_threshold = int(
            params.get("guest_compare_threshold", unusable_mem))
        if is_windows_guest:
            # Windows guest memory will be deducted from previous
            # unused memory instead of whole memory
            after_mem = get_win_mon_free_mem(session)
            act_threshold = ballooned_mem - after_mem - used_mem
        else:
            after_mem = vm.get_current_memory_size()
            act_threshold = ballooned_mem - after_mem
        if (after_mem > ballooned_mem) or (
                abs(act_threshold) > gcompare_threshold):
            test.fail("Balloon test failed")
    finally:
        vm.destroy()
        backup_xml.sync()
