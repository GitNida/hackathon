import unittest
from main import display_message

class TestDisplayMessage(unittest.TestCase):
    """
    Unit tests for the display_message function.
    """

    def test_display_message_output(self):
        """
        Test that the display_message function returns the correct string.
        """
        expected_message = "wow! we have done it :)"
        self.assertEqual(display_message(), expected_message)

if __name__ == "__main__":
    # Run the unit tests
    unittest.main()
