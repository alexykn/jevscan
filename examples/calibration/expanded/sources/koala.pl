package Service;

sub Service::send {
    my ($self, $message) = @_;
    return $self->{mailer}->send($message);
}

sub Service::retry {
    my ($self, $message, $attempts) = @_;
    return $self->{mailer}->retry($message, $attempts);
}

sub Service::status {
    my ($self) = @_;
    return $self->{mailer}->status;
}

1;
