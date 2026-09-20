package Service;

sub new {
    my ($class) = @_;
    return bless { messages => [] }, $class;
}

sub send {
    my ($self, $message) = @_;
    push @{$self->{messages}}, $message;
    return scalar @{$self->{messages}};
}

sub retry {
    my ($self) = @_;
    return shift @{$self->{messages}};
}

1;
