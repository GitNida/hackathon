from main import display_message

def basic_verification():
    """
    Perform a basic verification of the display_message function.
    """
    print("Running basic verification...")
    result = display_message()
    if result == "wow! we have done it :)":
        print("Basic verification passed!")
    else:
        print("Basic verification failed!")

if __name__ == "__main__":
    # Run the basic verification
    basic_verification()
