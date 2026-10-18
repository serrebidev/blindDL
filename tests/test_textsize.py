# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

import unittest

import wx

from blinddl.gui import textsize


class TextStepTests(unittest.TestCase):
    def test_up_and_down_step_through_the_offered_sizes(self):
        self.assertEqual(textsize.next_step(1.0, 1), 1.15)
        self.assertEqual(textsize.next_step(1.0, -1), 0.9)

    def test_stepping_stops_at_either_end(self):
        self.assertEqual(textsize.next_step(textsize.STEPS[-1], 1),
                         textsize.STEPS[-1])
        self.assertEqual(textsize.next_step(textsize.STEPS[0], -1),
                         textsize.STEPS[0])

    def test_a_hand_edited_size_lands_on_the_nearest_offered_one(self):
        self.assertEqual(textsize.nearest_step(1.44), 1.5)
        self.assertEqual(textsize.nearest_step("nonsense"), 1.0)
        self.assertEqual(textsize.nearest_step(None), 1.0)

    def test_the_size_is_read_out_as_a_percentage(self):
        self.assertEqual(textsize.describe(1.15), "115 percent")
        self.assertEqual(textsize.describe(1.0), "100 percent")

    def test_the_saved_size_comes_from_the_config(self):
        self.assertEqual(textsize.scale_for({"text_scale": 1.5}), 1.5)
        self.assertEqual(textsize.scale_for({}), 1.0)
        self.assertEqual(textsize.scale_for(None), 1.0)


class TextSizeWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def test_growing_and_shrinking_come_back_to_the_platform_size(self):
        # Sizes are worked out from what the platform gave the control, not
        # from whatever it was last set to, so up and then back down lands
        # where it started instead of somewhere four times as large.
        frame = wx.Frame(None)
        self.addCleanup(frame.Destroy)
        panel = wx.Panel(frame)
        label = wx.StaticText(panel, label="Text")
        base = label.GetFont().GetPointSize()

        textsize.apply(panel, 2.0)
        self.assertEqual(label.GetFont().GetPointSize(), base * 2)

        textsize.apply(panel, 1.0)
        self.assertEqual(label.GetFont().GetPointSize(), base)

    def test_every_control_in_a_window_is_reached(self):
        dialog = wx.Dialog(None)
        self.addCleanup(dialog.Destroy)
        outer = wx.Panel(dialog)
        inner = wx.StaticText(outer, label="Deep enough to be forgotten")
        base = inner.GetFont().GetPointSize()

        textsize.apply(dialog, 1.5)

        self.assertEqual(inner.GetFont().GetPointSize(),
                         max(6, int(round(base * 1.5))))

    def test_the_dialog_hook_is_installed_once(self):
        # Every dialog is fitted as it opens, rather than each of a dozen
        # dialogs remembering to ask, and a second install must not wrap
        # the first one again.
        textsize.install()
        first = wx.Dialog.ShowModal
        textsize.install()
        self.assertIs(wx.Dialog.ShowModal, first)


if __name__ == "__main__":
    unittest.main()
