package LeaseBook;

sub new {
    my ($class) = @_;
    return bless { leases => {} }, $class;
}

sub LeaseBook::claim {
    my ($self, $id, $owner) = @_;
    $self->{leases}{$id} = { owner => $owner, state => "held" };
}

sub LeaseBook::release {
    my ($self, $id) = @_;
    delete $self->{leases}{$id};
}

sub LeaseBook::expire {
    my ($self, $id) = @_;
    delete $self->{leases}{$id};
}

1;
