"""
This file contains code from streamrip (https://github.com/nathom/streamrip).
Streamrip is the property of nathom and multiple other contributors in the streamrip community.
Big thanks to nathom and the streamrip community for their incredible work.
"""
"""Streamrip specific exceptions."""


class AuthenticationError(Exception):
    """AuthenticationError."""


class MissingCredentialsError(Exception):
    """MissingCredentials."""


class IneligibleError(Exception):
    """IneligibleError.

    Raised when the account is not eligible to stream a track.
    """


class InvalidAppIdError(Exception):
    """InvalidAppIdError."""


class InvalidAppSecretError(Exception):
    """InvalidAppSecretError."""


class NonStreamableError(Exception):
    """Item is not streamable.

    A versatile error that can have many causes.
    """

    def __init__(self, message=None):
        """Create a NonStreamable exception.

        :param message:
        """
        self.message = message
        super().__init__(self.message)

    def print(self, item):
        """Print a readable version of the exception.

        :param item:
        """
        print(self.print_msg(item))

    def print_msg(self, item) -> str:
        """Return a generic readable message.

        :param item:
        :type item: Media
        :rtype: str
        """
        base_msg = [f"Unable to stream {item!s}."]
        if self.message:
            base_msg.extend(
                (
                    "Message:",
                    str(self.message),
                ),
            )

        return " ".join(base_msg)


class ConversionError(Exception):
    """ConversionError."""
