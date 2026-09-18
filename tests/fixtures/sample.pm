package Service;
use strict;
use warnings;

sub new {
    my ($class, $value) = @_;
    return bless { value => $value }, $class;
}
sub load {
    my ($self) = @_;
    return $self->{value};
}

package Other;
sub helper { return 1; }
1;
