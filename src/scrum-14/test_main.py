"""
Unit test module for main.py.
This module contains unit tests to verify the functionality of the display_message function.
"""

import unittest
from main import display_message


class TestDisplayMessage(unittest.TestCase):
    """
    Unit test class for the display_message function.
    """

    def test_display_message(self):
        """
        Test that the display_message function returns the correct message.
        """
        expected_message = "wow! we have done it :)"
        result = display_message()
        self.assertEqual(result, expected_message)


if __name__ == "__main__":
    # Run the unit tests
    unittest.main()
