use feature 'class';

class Counter {
    field $value = 0;
    method value { return $value; }
    method increment { ++$value; }
}
